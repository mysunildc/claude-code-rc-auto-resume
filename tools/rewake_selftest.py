"""Self-test: can an asyncRewake hook wake an IDLE session on your machine and Claude Code version?
自我測試：在你的電腦與 Claude Code 版本上，asyncRewake hook 能不能叫醒「閒置中」的 session？

This checks the mechanism the auto-resume hook relies on, without having to burn through a usage limit.
不必真的把額度用完，就能驗證自動接續依賴的機制。

  python tools/rewake_selftest.py arm      add a one-shot Stop hook / 加一個只觸發一次的 Stop hook
  (end the current Claude turn; ~20 s later Claude should be woken by "REWAKE SELFTEST OK")
  （結束目前這一輪；約 20 秒後 Claude 應被「REWAKE SELFTEST OK」叫醒）
  python tools/rewake_selftest.py disarm   remove it manually if it never fired / 沒觸發時手動移除

The hook removes itself from settings.json when it fires. 觸發時會自動把自己從 settings.json 移除。
Run it inside a Remote Control session to prove the Remote Control case. 在 Remote Control session 中跑，就能驗證 Remote Control 的情況。
It is a global Stop hook: if several sessions are open, whichever ends a turn first uses it up.
這是全域 Stop hook：同時開著多個 session 時，最先結束回合的那個會用掉它。
"""
import json
import os
import sys
import time
from pathlib import Path

SELF = Path(__file__).resolve()
SETTINGS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
TAG = "rewake_selftest.py"
DELAY = 20


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def load():
    return json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}


def save(s):
    tmp = SETTINGS.with_name("settings.json.tmp-selftest")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS)


def remove_entry(s):
    groups = s.get("hooks", {}).get("Stop", [])
    kept = [g for g in groups if not any(TAG in str(h.get("command", "")) for h in g.get("hooks", []))]
    if len(kept) == len(groups):
        return False
    if kept:
        s["hooks"]["Stop"] = kept
    else:
        del s["hooks"]["Stop"]
        if not s["hooks"]:
            del s["hooks"]
    return True


def arm():
    s = load()
    remove_entry(s)
    cmd = f"{sh_quote(Path(sys.executable).as_posix())} {sh_quote(SELF.as_posix())} fire"
    s.setdefault("hooks", {}).setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": cmd, "asyncRewake": True, "timeout": 120}]})
    save(s)
    print(f"Armed. End this turn; Claude should be woken in about {DELAY} s. "
          f"/ 已啟用，結束這一輪，約 {DELAY} 秒後 Claude 應被叫醒。")


def disarm():
    s = load()
    if remove_entry(s):
        save(s)
        print("Removed. / 已移除。")
    else:
        print("Nothing to remove. / 沒有需要移除的。")


def fire():
    sys.stdin.buffer.read()
    s = load()
    if remove_entry(s):  # one-shot: only the first Stop after arming wakes Claude / 只觸發一次
        save(s)
    else:
        return 0
    time.sleep(DELAY)
    sys.stderr.buffer.write("REWAKE SELFTEST OK: an asyncRewake hook woke this idle session. "
                            "This is a test, not a user instruction; just confirm you received it. "
                            "/ 自我測試成功：asyncRewake 叫醒了閒置的 session。".encode("utf-8"))
    sys.stderr.flush()
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # bilingual output on any Windows code page / 任何 code page 都能輸出雙語
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "arm":
        arm()
    elif cmd == "disarm":
        disarm()
    elif cmd == "fire":
        sys.exit(fire())
    else:
        print(__doc__)
