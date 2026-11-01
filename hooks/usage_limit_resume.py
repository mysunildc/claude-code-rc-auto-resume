"""Claude Code StopFailure hook: auto-resume a session after the subscription usage limit resets.
Claude Code StopFailure hook：訂閱額度用完後，等到重置時間自動叫醒 Claude 繼續工作。

Why / 為什麼需要：
  Claude Code's built-in "wait for the limit to reset, then continue" does not start in
  Remote Control sessions (documented). This hook fills that gap.
  內建的「等額度重置後自動繼續」在 Remote Control session 不會啟動（官方文件寫明），這支 hook 補上這個缺口。

Flow / 流程：
  1. Claude Code runs this hook (asyncRewake, in the background) when a turn fails with error "rate_limit".
     回合因 rate_limit 失敗時，Claude Code 在背景執行本 hook。
  2. Reset time: first from the status line snapshot (exact epoch, the same data as the "approaching limit"
     warning), otherwise parsed from the error text, e.g. "You've hit your session limit · resets 3:45pm".
     重置時間優先取狀態列存下的精確時間戳記，取不到才解析錯誤訊息文字。
  3. Sleep until reset + 90 s. Skip waking if someone already continued the conversation, or if a newer
     wait for the same session took over.
     睡到「重置時間 + 90 秒」；若期間已有人接手對話，或同一 session 有更新的等待，就不叫醒。
  4. Wake Claude: write a reminder to stderr and exit 2 (asyncRewake starts a new turn even when idle).
     叫醒：把提醒寫到 stderr 並 exit 2（asyncRewake 會開新回合，閒置時也會）。

The status line snapshot is written by a small wrapper that install.py adds in front of your status line command.
狀態列快照由 install.py 加在你原本狀態列指令前面的一小段 shell 寫入。

Python 3.8+, standard library only. / 只用標準函式庫。
"""
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path


def state_dir():
    """Where logs / markers / the status line snapshot live. 放 log、marker、狀態列快照的目錄。"""
    override = os.environ.get("CLAUDE_AUTO_RESUME_DIR")
    if override:
        return Path(override)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ClaudeUsageResume"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "claude-usage-resume"


WORK = state_dir()
LOG_FILE = WORK / "resume.log"
STATUSLINE_FILE = WORK / "statusline-last.json"
STATUSLINE_MAX_AGE = 6 * 3600               # seconds; older snapshots are ignored / 超過就不用
MARGIN = dt.timedelta(seconds=90)           # wait a bit past the reset / 重置後再多等一下
UNKNOWN_FIRST = dt.timedelta(minutes=30)    # retry interval when no reset time is found, doubled each time / 解析不到時的重試間隔，逐次倍增
UNKNOWN_CAP = dt.timedelta(hours=8)
UNKNOWN_MAX_TRIES = 25
MAX_WAIT = dt.timedelta(days=7, hours=12)   # must stay below the hook timeout (691200 s = 8 days) / 要小於 hook timeout
MIN_WAIT = dt.timedelta(seconds=30)        # floor, even if the reset time already passed / 下限
RECENT_WAKE = dt.timedelta(minutes=15)     # limited again this soon after our wake: distrust reset times / 剛叫醒又撞上限
STATE_MAX_AGE = 9 * 86400                   # seconds; marker/state files older than this are deleted / 舊狀態檔清除
POLL_SECONDS = 30                           # sleep in slices so a laptop waking from sleep still checks the wall clock / 分段睡

LIMIT_HINTS = ("hit your", "out of usage", "usage limit", "resets ")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # matches datetime.weekday()
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

WAKE_MESSAGE = (
    "The usage limit should have reset now (reset time {when}). This reminder comes from the auto-resume hook, "
    "not from the user. Continue the work that was interrupted by the usage limit. If that work is actually "
    "finished, briefly report the current status.")

logging.raiseExceptions = False  # never print logging tracebacks to stderr (stderr goes to Claude) / stderr 會送給 Claude
log = logging.getLogger("usage_limit_resume")


def log_open():
    WORK.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=6, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def log_close():
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()


def load_zone(name):
    """IANA zone or None. On Windows, Python has no tz database unless the `tzdata` package is installed.
    取得時區；Windows 的 Python 沒裝 tzdata 時取不到。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def parse_reset(msg, now):
    """Parse 'resets 8:30pm (Asia/Taipei)' / 'resets Nov 6, 9am' / 'resets Mon 12:00am' / 'resets 20:30'
    into local naive time (`now` is local naive). Returns None when not parseable.
    When the message names a time zone that can be loaded, the date arithmetic ("already past → tomorrow",
    weekday) is done in that zone, then converted to local. If the zone can't be loaded, the time is taken as
    local: Claude Code shows reset times in the system zone by default.
    訊息帶時區且能載入時，日期推算在該時區內完成再換成本機；載入不到就當本機時間（Claude Code 預設用系統時區顯示）。"""
    low = msg.lower()
    tzm = re.search(r"\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)+)\)", msg[low.find("resets"):]) if "resets" in low else None
    zone = load_zone(tzm["tz"]) if tzm else None
    ref = now.astimezone(zone).replace(tzinfo=None) if zone else now
    t = parse_reset_naive(msg, ref)
    if t is None or zone is None:
        return t
    return t.replace(tzinfo=zone).astimezone().replace(tzinfo=None)


def parse_reset_naive(msg, now):
    """parse_reset without time zone handling: `now` and the result are in the message's own zone.
    不處理時區的解析：now 與回傳值都在訊息本身的時區。"""
    wd = re.search(r"resets\s+(?:at\s+|on\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+(?:at\s+)?", msg, re.I)
    if wd:  # weekly limit uses a weekday: parse the time, then move to the next such weekday / 週額度用星期幾
        t = parse_reset_naive(msg[:wd.start()] + "resets " + msg[wd.end():], now)
        if t is None:
            return None
        t = t.replace(year=now.year, month=now.month, day=now.day)
        t += dt.timedelta(days=(WEEKDAYS.index(wd.group(1).lower()) - now.weekday()) % 7)
        if t < now - dt.timedelta(minutes=5):
            t += dt.timedelta(days=7)
        return t
    m = re.search(r"resets\s+(?:at\s+)?"
                  r"(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
                  r"(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ap>am|pm)?"
                  r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*)\))?", msg, re.I)
    if not m:
        return None
    hour, minute, ap = int(m["h"]), int(m["min"] or 0), (m["ap"] or "").lower()
    if ap:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ap == "pm" else 0)
    elif m["min"] is None:  # "resets 3" without am/pm or minutes: don't guess / 不猜
        return None
    if hour > 23 or minute > 59:
        return None
    try:
        if m["mon"]:
            mon = MONTHS.get(m["mon"][:3].lower())
            if not mon:
                return None
            t = dt.datetime(now.year, mon, int(m["day"]), hour, minute)
            if t < now - dt.timedelta(days=1):  # December seeing "Jan" / 跨年
                t = t.replace(year=now.year + 1)
        else:
            t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if t < now - dt.timedelta(minutes=5):  # time only and already past: tomorrow / 已過就是明天
                t += dt.timedelta(days=1)
    except ValueError:  # e.g. Feb 29 in a non-leap year / 不存在的日期
        return None
    return t


def limit_window(msg):
    """Which limit was hit: 'seven_day', 'five_hour', or None. 判斷撞到哪個額度。"""
    low = msg.lower()
    if "week" in low or "7-day" in low:
        return "seven_day"
    if "5-hour" in low or "five-hour" in low or "session limit" in low:
        return "five_hour"
    return None


def reset_from_statusline(msg, now):
    """Exact reset time from the status line snapshot (rate_limits.<window>.resets_at, epoch seconds).
    從狀態列快照取精確重置時間；檔案過舊、時間已過、看不出是哪個額度時回傳 None。"""
    try:
        if time.time() - STATUSLINE_FILE.stat().st_mtime > STATUSLINE_MAX_AGE:
            return None
        limits = json.loads(STATUSLINE_FILE.read_text(encoding="utf-8")).get("rate_limits") or {}
    except (OSError, ValueError, AttributeError):
        return None
    win = limit_window(msg)
    if win is None:  # message doesn't say which: use the one that is full / 訊息沒寫：取已用滿的那個
        full = [k for k in ("five_hour", "seven_day")
                if isinstance(limits.get(k), dict) and (limits[k].get("used_percentage") or 0) >= 100]
        win = full[0] if len(full) == 1 else None
    ts = (limits.get(win) or {}).get("resets_at") if win else None
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    t = dt.datetime.fromtimestamp(ts)
    return t if t > now - dt.timedelta(minutes=5) else None


def is_real_activity(e):
    """Does this transcript entry mean someone continued the conversation? 這筆逐字稿條目算不算有人接手。"""
    if e.get("isMeta") or e.get("isApiErrorMessage") or e.get("isCompactSummary"):
        return False
    if e.get("type") == "assistant":
        return True
    if e.get("type") != "user" or "toolUseResult" in e:
        return False
    content = (e.get("message") or {}).get("content")
    if isinstance(content, list):  # tool_result-only entries don't count / 只有 tool_result 的不算
        return any(isinstance(c, dict) and c.get("type") != "tool_result" for c in content)
    return bool(content)


def activity_after(transcript, since_utc):
    """True if the transcript has a real user message or Claude reply after since_utc (reads the last 2 MB).
    逐字稿在 since_utc 之後是否已有新的使用者訊息或 Claude 回覆。"""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2 * 1024 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in tail:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or not e.get("timestamp") or not is_real_activity(e):
            continue
        try:
            t = dt.datetime.fromisoformat(str(e["timestamp"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        if t > since_utc:
            return True
    return False


def retry_io(fn, default=None):
    """Retry briefly on transient errors: on Windows a file another process is reading/deleting raises
    PermissionError for a moment. FileNotFoundError is final. 短暫重試：Windows 上檔案正被別的程序讀取或刪除時會暫時 PermissionError。"""
    for _ in range(10):
        try:
            return fn()
        except FileNotFoundError:
            return default
        except OSError:
            time.sleep(0.2)
    return default


def is_latest(marker, token):
    """Only the newest waiter of a session may wake it; a missing marker means step aside.
    同一 session 只讓最新的一支叫醒；marker 不存在就讓位。"""
    return retry_io(lambda: marker.read_text(encoding="utf-8")) == token


def cleanup_old_state():
    """Delete per-session marker/state files older than STATE_MAX_AGE. 清掉過舊的每個 session 狀態檔。"""
    for pattern in ("waiting-*.txt", "woke-*.txt", "unparsed-*.txt"):
        for f in WORK.glob(pattern):
            retry_io(lambda: f.unlink() if time.time() - f.stat().st_mtime > STATE_MAX_AGE else None)


def last_wake(woke):
    """{"at": epoch of our last wake-up, "reset": epoch of the reset time it used or None}, or None.
    上次叫醒的時間與當時採用的重置時間。"""
    try:
        d = json.loads(retry_io(lambda: woke.read_text(encoding="utf-8")) or "")
        return d if isinstance(d, dict) and isinstance(d.get("at"), (int, float)) else None
    except ValueError:
        return None


def unknown_delay(state):
    """Back-off when no reset time is found; None after UNKNOWN_MAX_TRIES. 解析不到時的退避間隔。"""
    try:
        n = int(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        n = 0
    if n >= UNKNOWN_MAX_TRIES:
        return None
    state.write_text(str(n + 1), encoding="utf-8")
    return min(UNKNOWN_FIRST * (2 ** n), UNKNOWN_CAP)


def main():
    log_open()
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        log.error("stdin is not JSON, ignored")
        return 0
    msg = str(data.get("last_assistant_message") or "")
    sid = str(data.get("session_id") or "unknown")
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)
    transcript = data.get("transcript_path") or ""
    if data.get("error") != "rate_limit" or not any(k in msg.lower() for k in LIMIT_HINTS):
        log.info("[%s] not a usage limit (error=%s, message=%r), ignored", sid[:8], data.get("error"), msg[:120])
        return 0

    cleanup_old_state()
    failed_at = dt.datetime.now(dt.timezone.utc)
    now = dt.datetime.now()
    woke = WORK / f"woke-{safe_sid}.txt"
    state = WORK / f"unparsed-{safe_sid}.txt"
    reset, source = reset_from_statusline(msg, now), "status line"
    if reset is None:
        reset, source = parse_reset(msg, now), "error message"
    last = last_wake(woke)
    if reset is not None and last and time.time() - last["at"] < RECENT_WAKE.total_seconds() \
            and last.get("reset") is not None and reset.timestamp() <= last["reset"] + 120:
        # We woke this session minutes ago using (about) this same reset time and it is still limited: that time
        # is proven wrong (clock skew, DST, wrong zone). A different, later reset (e.g. the weekly limit right after
        # the 5-hour one) is still trusted.
        # 剛用（約）同一個重置時間叫醒，卻仍被限制：這個時間證實不準，改用退避。換成更晚的重置時間（例如緊接著撞上週額度）照常採用。
        reset, source = None, f"ignored (woke this session {int(time.time() - last['at'])} s ago for the same reset)"
    if reset is None:
        delay = unknown_delay(state)
        if delay is None:
            log.error("[%s] no reset time after %d tries (%r), giving up", sid[:8], UNKNOWN_MAX_TRIES, msg[:160])
            return 0
        wake_at = now + delay
        log.warning("[%s] no usable reset time (%s): %r; retrying at %s", sid[:8],
                    source if source.startswith("ignored") else "not found", msg[:160], wake_at.isoformat(timespec="seconds"))
    else:
        state.unlink(missing_ok=True)
        wake_at = max(reset + MARGIN, now + MIN_WAIT)  # never wake instantly / 不立刻叫醒
    if wake_at - now > MAX_WAIT:
        log.error("[%s] reset %s is more than %s away, not waiting", sid[:8], reset, MAX_WAIT)
        return 0

    # Markers are never deleted here (that would race with a newer waiter); cleanup_old_state removes old ones.
    # marker 不在這裡刪（會和更新的等待者競態），由 cleanup_old_state 清掉舊檔。
    token = f"{os.getpid()}-{time.time_ns()}"
    marker = WORK / f"waiting-{safe_sid}.txt"
    retry_io(lambda: marker.write_text(token, encoding="utf-8"))
    if reset:
        log.info("[%s] usage limit hit: %r; reset source=%s; will wake at %s", sid[:8], msg[:160], source,
                 wake_at.isoformat(timespec="seconds"))
    log_close()  # don't hold the log file open while waiting / 等待期間不占用 log 檔
    wake_ts = wake_at.timestamp()  # wait on epoch time so DST changes don't matter / 用 epoch 等，不受日光節約影響
    while time.time() < wake_ts:
        time.sleep(min(POLL_SECONDS, max(1, wake_ts - time.time())))
        if not is_latest(marker, token):
            break
    log_open()
    if not is_latest(marker, token):
        log.info("[%s] a newer wait took over, exiting", sid[:8])
        return 0
    if transcript and activity_after(transcript, failed_at):
        log.info("[%s] conversation already continued after the failure, not waking", sid[:8])
        return 0

    record = json.dumps({"at": time.time(), "reset": reset.timestamp() if reset else None})
    retry_io(lambda: woke.write_text(record, encoding="utf-8"))
    when = reset.strftime("%m-%d %H:%M") if reset else "unknown, retrying"
    # Write UTF-8 bytes: on Windows a piped stderr defaults to the ANSI code page and non-ASCII text gets garbled.
    # 一定要寫 UTF-8 位元組：Windows 管線下 stderr 預設是 ANSI code page，非 ASCII 字會變亂碼。
    sys.stderr.buffer.write(WAKE_MESSAGE.format(when=when).encode("utf-8"))
    sys.stderr.flush()
    log.info("[%s] woke Claude to continue", sid[:8])
    return 2


if __name__ == "__main__":
    sys.exit(main())
