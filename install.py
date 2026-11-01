"""Install / uninstall the usage-limit auto-resume hook into Claude Code user settings.
把額度上限自動接續 hook 安裝到（或移出）Claude Code 的使用者設定。

Usage / 用法：
  python install.py               install (or update) / 安裝或更新
  python install.py --dry-run     show what would change, write nothing / 只顯示會改什麼
  python install.py --no-statusline   don't wrap the status line / 不包裝狀態列
  python install.py --uninstall   remove everything this script added / 移除本腳本加入的設定

What it changes / 會改的東西：
  1. Copies hooks/usage_limit_resume.py to <claude dir>/hooks/.
  2. Adds a StopFailure hook (matcher "rate_limit", asyncRewake) to <claude dir>/settings.json.
  3. If you already have a command status line, prefixes it with a tiny shell snippet that saves the
     status line JSON (it contains the exact reset times). Your status line output is unchanged.
     若已有指令型狀態列，在前面加一小段 shell 存下狀態列 JSON（含精確重置時間），顯示內容不變。
A timestamped backup of settings.json is written before any change. 修改前會先備份 settings.json。
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_SRC = HERE / "hooks" / "usage_limit_resume.py"
HOOK_NAME = "usage_limit_resume.py"
TIMEOUT = 691200  # 8 days; the hook gives up after 7.5 days / 8 天，hook 本身 7.5 天就放棄
WRAP_TAG = "# claude-code-rc-auto-resume"
WRAP_RE = re.compile(r"^IFS= read -r -d '' cc_sl_in; .*?\| \{\n(?P<orig>.*)\n\} " + re.escape(WRAP_TAG) + r"$", re.S)


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def state_dir():
    """Same directory the hook uses (imported from the hook so the two never disagree). 與 hook 用同一個目錄。"""
    spec = importlib.util.spec_from_file_location("usage_limit_resume", HOOK_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.WORK


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def hook_command(hook_path):
    # single quotes: paths with $, ` or " stay literal in bash / 單引號，路徑含特殊字元也不會被 bash 展開
    return f"{sh_quote(Path(sys.executable).as_posix())} {sh_quote(hook_path.as_posix())}"


def is_ours(h):
    return isinstance(h, dict) and HOOK_NAME in str(h.get("command", ""))


def strip_hook(settings):
    """Remove our StopFailure entries; drop empty groups. 移除本工具加的 StopFailure 項目。"""
    groups = settings.get("hooks", {}).get("StopFailure")
    if not isinstance(groups, list):
        return
    kept = []
    for g in groups:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            g = {**g, "hooks": [h for h in g["hooks"] if not is_ours(h)]}
            if not g["hooks"]:
                continue
        kept.append(g)
    if kept:
        settings["hooks"]["StopFailure"] = kept
    else:
        del settings["hooks"]["StopFailure"]
        if not settings["hooks"]:
            del settings["hooks"]


def wrap_statusline(cmd, work):
    d = sh_quote(work.as_posix())
    return ("IFS= read -r -d '' cc_sl_in; "
            f"mkdir -p {d} && printf '%s' \"$cc_sl_in\" > {d}/statusline.$$.tmp "
            f"&& mv -f {d}/statusline.$$.tmp {d}/statusline-last.json; "
            "printf '%s' \"$cc_sl_in\" | {\n" + cmd + "\n} " + WRAP_TAG)


def unwrap_statusline(cmd):
    m = WRAP_RE.match(cmd)
    return m["orig"] if m else cmd


def use_utf8_output():
    """Messages are bilingual; a piped stdout on Windows uses the ANSI code page (cp1252 can't encode Chinese).
    訊息是中英雙語；Windows 管線下 stdout 用 ANSI code page，英文系統會無法輸出中文。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    use_utf8_output()  # before argparse: --help prints the bilingual docstring / 要在 argparse 之前，--help 會印雙語說明
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-statusline", action="store_true")
    a = ap.parse_args()
    if sys.version_info < (3, 8):
        sys.exit("Python 3.8+ is required / 需要 Python 3.8 以上")

    cdir = claude_dir()
    settings_path = cdir / "settings.json"
    hook_dst = cdir / "hooks" / HOOK_NAME
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except ValueError as e:
        sys.exit(f"{settings_path} is not valid JSON, fix it first / 不是合法 JSON，請先修正：{e}")
    before = json.dumps(settings, indent=2, ensure_ascii=False)
    work = state_dir()
    notes = []

    strip_hook(settings)
    sl = settings.get("statusLine")
    if isinstance(sl, dict) and isinstance(sl.get("command"), str):
        sl["command"] = unwrap_statusline(sl["command"])

    if not a.uninstall:
        settings.setdefault("hooks", {}).setdefault("StopFailure", []).append({
            "matcher": "rate_limit",
            "hooks": [{"type": "command", "command": hook_command(hook_dst), "asyncRewake": True, "timeout": TIMEOUT}],
        })
        if a.no_statusline:
            notes.append("status line not wrapped (--no-statusline): reset time comes from the error text only")
        elif isinstance(sl, dict) and sl.get("type") == "command" and isinstance(sl.get("command"), str):
            sl["command"] = wrap_statusline(sl["command"], work)
            notes.append("status line wrapped to save exact reset times / 狀態列已包裝，會存下精確重置時間")
        else:
            notes.append("no command status line found: reset time comes from the error text only "
                         "/ 沒有指令型狀態列，重置時間改由錯誤訊息解析")

    after = json.dumps(settings, indent=2, ensure_ascii=False)
    print(f"Claude dir / 設定目錄: {cdir}")
    print(f"State dir (logs) / 狀態與 log 目錄: {work}")
    if a.dry_run:
        print("--- settings.json after change (dry run) / 修改後內容（未寫入）---")
        print(after)
        return 0

    if after != before:
        if settings_path.exists():
            backup = settings_path.with_name(f"settings.json.bak-{dt.datetime.now():%Y%m%d%H%M%S}")
            shutil.copy2(settings_path, backup)
            print(f"Backup / 備份: {backup}")
        cdir.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_name("settings.json.tmp-auto-resume")
        tmp.write_text(after + "\n", encoding="utf-8")
        os.replace(tmp, settings_path)
    if a.uninstall:
        hook_dst.unlink(missing_ok=True)
        print("Uninstalled. Logs are kept in the state dir. / 已移除，log 保留在狀態目錄。")
    else:
        hook_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HOOK_SRC, hook_dst)
        print(f"Hook installed / 已安裝: {hook_dst}")
        for n in notes:
            print("- " + n)
        print("Open /hooks once or restart Claude Code to be sure the new hook is loaded. "
              "/ 開一次 /hooks 或重開 Claude Code，確保新 hook 已載入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
