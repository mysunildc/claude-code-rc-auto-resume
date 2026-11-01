"""Tests for the hook and the installer. Run: python -m unittest discover -s tests -v
hook 與安裝程式的測試。執行：python -m unittest discover -s tests -v
End-to-end cases start the hook as a real subprocess and take about 10-20 s each.
端對端測試會實際啟動 hook 子程序，每項約 10-20 秒。
"""
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "usage_limit_resume.py"
INSTALL = ROOT / "install.py"
TMP = Path(tempfile.mkdtemp(prefix="auto-resume-test-"))
os.environ["CLAUDE_AUTO_RESUME_DIR"] = str(TMP / "state")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


h = load(HOOK, "hook")
inst = load(INSTALL, "install")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class ParseReset(unittest.TestCase):
    now = dt.datetime(2026, 11, 4, 22, 0)  # Wednesday 22:00

    def check(self, msg, expected):
        self.assertEqual(h.parse_reset(msg, self.now), expected, msg)

    def test_formats(self):
        D = dt.datetime
        self.check("You've hit your 5-hour limit · resets 11:30pm", D(2026, 11, 4, 23, 30))
        self.check("You've hit your session limit · resets 3:45pm", D(2026, 11, 5, 15, 45))  # already past -> tomorrow
        self.check("resets 8pm", D(2026, 11, 5, 20, 0))
        self.check("resets 12am", D(2026, 11, 5, 0, 0))
        self.check("resets 12pm", D(2026, 11, 5, 12, 0))
        self.check("resets at 23:10", D(2026, 11, 4, 23, 10))
        self.check("resets 21:58", D(2026, 11, 4, 21, 58))  # within 5 min tolerance -> today
        self.check("You've hit your weekly limit · resets Nov 6, 9am", D(2026, 11, 6, 9, 0))
        self.check("You've hit your weekly limit · resets Mon 12:00am", D(2026, 11, 9, 0, 0))
        self.check("resets Wed 11pm", D(2026, 11, 4, 23, 0))
        self.check("resets Wed 9pm", D(2026, 11, 11, 21, 0))
        self.check("resets Thursday at 1:30am (Asia/Taipei)", D(2026, 11, 5, 1, 30))

    def test_year_rollover(self):
        now = dt.datetime(2026, 12, 30, 10, 0)
        self.assertEqual(h.parse_reset("resets Jan 2, 9am", now), dt.datetime(2027, 1, 2, 9, 0))

    def test_unparseable(self):
        for msg in ["resets 3", "resets 13pm", "resets 25:00", "resets Foo 3, 9am", "resets Mon",
                    "resets Feb 30, 9am", "You're out of usage", ""]:
            self.check(msg, None)


class Statusline(unittest.TestCase):
    def setUp(self):
        h.STATUSLINE_FILE = TMP / "sl.json"
        self.now = dt.datetime.now()
        self.f5 = int(time.time()) + 3600
        self.f7 = int(time.time()) + 2 * 86400

    def write(self, rl, age=0):
        h.STATUSLINE_FILE.write_text(json.dumps({"rate_limits": rl}), encoding="utf-8")
        if age:
            t = time.time() - age
            os.utime(h.STATUSLINE_FILE, (t, t))

    def test_window(self):
        self.assertEqual(h.limit_window("You've hit your 5-hour limit"), "five_hour")
        self.assertEqual(h.limit_window("You've hit your session limit"), "five_hour")
        self.assertEqual(h.limit_window("You've hit your weekly limit"), "seven_day")
        self.assertIsNone(h.limit_window("You're out of usage"))

    def test_pick(self):
        F = dt.datetime.fromtimestamp
        self.write({"five_hour": {"used_percentage": 100, "resets_at": self.f5},
                    "seven_day": {"used_percentage": 60, "resets_at": self.f7}})
        self.assertEqual(h.reset_from_statusline("hit your session limit", self.now), F(self.f5))
        self.assertEqual(h.reset_from_statusline("hit your weekly limit", self.now), F(self.f7))
        self.assertEqual(h.reset_from_statusline("out of usage", self.now), F(self.f5))

    def test_rejects(self):
        self.write({"five_hour": {"used_percentage": 100, "resets_at": self.f5}}, age=7 * 3600)
        self.assertIsNone(h.reset_from_statusline("hit your 5-hour limit", self.now))  # stale file
        self.write({"five_hour": {"used_percentage": 100, "resets_at": int(time.time()) - 3600}})
        self.assertIsNone(h.reset_from_statusline("hit your 5-hour limit", self.now))  # already past
        self.write({"five_hour": {"used_percentage": 100, "resets_at": self.f5},
                    "seven_day": {"used_percentage": 100, "resets_at": self.f7}})
        self.assertIsNone(h.reset_from_statusline("out of usage", self.now))  # ambiguous
        self.write({"five_hour": None})
        self.assertIsNone(h.reset_from_statusline("hit your 5-hour limit", self.now))
        h.STATUSLINE_FILE.write_text("not json", encoding="utf-8")
        self.assertIsNone(h.reset_from_statusline("hit your 5-hour limit", self.now))
        h.STATUSLINE_FILE.unlink()
        self.assertIsNone(h.reset_from_statusline("hit your 5-hour limit", self.now))


class Transcript(unittest.TestCase):
    def test_activity(self):
        t0 = dt.datetime.now(dt.timezone.utc)
        later = (t0 + dt.timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        earlier = (t0 - dt.timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        p = TMP / "t.jsonl"

        def run(*entries):
            p.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n", encoding="utf-8")
            return h.activity_after(str(p), t0)

        self.assertFalse(run({"type": "assistant", "timestamp": earlier}))
        self.assertFalse(run({"type": "assistant", "isApiErrorMessage": True, "timestamp": later}))
        self.assertFalse(run({"type": "user", "isMeta": True, "message": {"content": "x"}, "timestamp": later}))
        self.assertFalse(run({"type": "user", "toolUseResult": {}, "message": {"content": "x"}, "timestamp": later}))
        self.assertFalse(run({"type": "user", "timestamp": later,
                              "message": {"content": [{"type": "tool_result", "content": "x"}]}}))
        self.assertTrue(run({"type": "user", "message": {"content": "continue"}, "timestamp": later}))
        self.assertTrue(run({"type": "assistant", "timestamp": later}))
        self.assertTrue(run({"type": "assistant", "timestamp": later.replace("Z", "")}))  # naive = UTC
        self.assertFalse(h.activity_after(str(TMP / "missing.jsonl"), t0))

    def test_backoff(self):
        st = TMP / "unparsed.txt"
        st.unlink(missing_ok=True)
        delays = [h.unknown_delay(st) for _ in range(h.UNKNOWN_MAX_TRIES)]
        self.assertEqual(delays[0], dt.timedelta(minutes=30))
        self.assertEqual(delays[-1], h.UNKNOWN_CAP)
        self.assertIsNone(h.unknown_delay(st))


def run_hook(payload, env_dir, wait=True):
    env = {**os.environ, "CLAUDE_AUTO_RESUME_DIR": str(env_dir)}
    p = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, env=env)
    p.stdin.write(json.dumps(payload).encode("utf-8"))
    p.stdin.close()
    if not wait:
        return p
    _, err = p.communicate(timeout=120)
    return p.returncode, err.decode("utf-8")


def just_passed(seconds=80):
    """A reset time `seconds` ago, so the hook wakes about 90 - seconds later. 讓 hook 很快醒來的重置時間。"""
    return (dt.datetime.now() - dt.timedelta(seconds=seconds)).strftime("%H:%M")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=TMP))

    def snapshot(self, resets_at):
        self.d.mkdir(parents=True, exist_ok=True)
        (self.d / "statusline-last.json").write_text(json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": resets_at}}}), encoding="utf-8")

    def test_ignores_other_errors(self):
        code, err = run_hook({"session_id": "a", "error": "overloaded", "last_assistant_message": "resets 3pm"}, self.d)
        self.assertEqual((code, err), (0, ""))

    def test_wakes_from_statusline(self):
        self.snapshot(int(time.time()) - 80)
        start = time.time()
        code, err = run_hook({"session_id": "b", "error": "rate_limit",
                              "last_assistant_message": "You've hit your 5-hour limit · resets 3am"}, self.d)
        self.assertEqual(code, 2)
        self.assertIn("usage limit should have reset", err)
        self.assertLess(time.time() - start, 40)  # used the snapshot, not "3am" / 用的是快照，不是 3am
        self.assertIn("reset source=status line", (self.d / "resume.log").read_text(encoding="utf-8"))
        self.assertTrue((self.d / "woke-b.txt").exists())  # remembered for the recent-wake guard / 記下叫醒時間

    def test_wakes_from_text(self):
        # minute precision: up to 60 s extra / 文字只到分鐘，最多多等 60 秒
        code, err = run_hook({"session_id": "c", "error": "rate_limit",
                              "last_assistant_message": f"You've hit your session limit · resets {just_passed(89)}"}, self.d)
        self.assertEqual(code, 2)
        self.assertIn("reset source=error message", (self.d / "resume.log").read_text(encoding="utf-8"))

    def test_newer_wait_takes_over(self):
        # Both wake in the same second, so this also exercises Windows file-sharing races. Repeated 3 times.
        # 兩支在同一秒醒來，順便測 Windows 檔案共用的競態；重複 3 次。
        for i in range(3):
            self.snapshot(int(time.time()) - 75)
            msg = {"session_id": f"d{i}", "error": "rate_limit", "last_assistant_message": "You've hit your 5-hour limit"}
            first = run_hook(msg, self.d, wait=False)
            time.sleep(2)
            second = run_hook(msg, self.d, wait=False)
            r1, r2 = first.wait(timeout=120), second.wait(timeout=120)
            err = (first.stderr.read() + second.stderr.read()).decode("utf-8", "replace")
            self.assertEqual((r1, r2), (0, 2), err)  # older steps aside, newest wakes / 舊的讓位、最新的叫醒

    def test_no_wake_when_continued(self):
        self.snapshot(int(time.time()) - 80)
        tr = self.d / "tr.jsonl"
        p = run_hook({"session_id": "e", "error": "rate_limit", "transcript_path": str(tr),
                      "last_assistant_message": "You've hit your 5-hour limit"}, self.d, wait=False)
        time.sleep(2)
        tr.write_text(json.dumps({"type": "user", "message": {"content": "go on"},
                                  "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()}) + "\n", encoding="utf-8")
        self.assertEqual(p.wait(timeout=120), 0)


class Installer(unittest.TestCase):
    ORIG_SL = 'input=$(cat); echo "my status: $(printf %s "$input" | head -c 20)"'

    def setUp(self):
        self.cdir = Path(tempfile.mkdtemp(dir=TMP))
        self.settings = self.cdir / "settings.json"
        self.base = {"model": "opus", "hooks": {"StopFailure": [{"matcher": "x", "hooks": [
            {"type": "command", "command": "echo other"}]}]},
            "statusLine": {"type": "command", "command": self.ORIG_SL}}
        self.settings.write_text(json.dumps(self.base), encoding="utf-8")

    def run_install(self, *args):
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.cdir), "CLAUDE_AUTO_RESUME_DIR": str(self.cdir / "state")}
        r = subprocess.run([sys.executable, str(INSTALL), *args], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_idempotent_uninstall(self):
        s = self.run_install()
        s = self.run_install()  # twice: no duplicates / 裝兩次不重複
        groups = s["hooks"]["StopFailure"]
        ours = [g for g in groups if g.get("matcher") == "rate_limit"]
        self.assertEqual(len(ours), 1)
        hook = ours[0]["hooks"][0]
        self.assertTrue(hook["asyncRewake"])
        self.assertEqual(hook["timeout"], 691200)
        self.assertTrue((self.cdir / "hooks" / "usage_limit_resume.py").exists())
        self.assertIn("echo other", json.dumps(groups))  # other hooks untouched / 其他 hook 不動
        self.assertTrue(s["statusLine"]["command"].endswith(inst.WRAP_TAG))
        self.assertTrue(list(self.cdir.glob("settings.json.bak-*")))
        s = self.run_install("--uninstall")
        self.assertEqual(s, self.base)  # exactly restored / 完整還原
        self.assertFalse((self.cdir / "hooks" / "usage_limit_resume.py").exists())

    def test_dry_run_writes_nothing(self):
        before = self.settings.read_text(encoding="utf-8")
        self.run_install("--dry-run")
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)

    def test_no_statusline_flag(self):
        s = self.run_install("--no-statusline")
        self.assertEqual(s["statusLine"]["command"], self.ORIG_SL)

    # Full path: on Windows a bare "bash" can resolve to WSL's System32\bash.exe. 用完整路徑，避免 Windows 抓到 WSL 的 bash
    BASH = shutil.which("bash")

    @unittest.skipUnless(BASH and "system32" not in BASH.lower(), "Git Bash / bash not found")
    def test_wrapped_statusline_runs(self):
        state = self.cdir / "state"
        wrapped = inst.wrap_statusline(self.ORIG_SL, state)
        data = json.dumps({"rate_limits": {"five_hour": {"resets_at": 123}}, "pad": "x" * 50})
        r = subprocess.run([self.BASH, "-c", wrapped], input=data.encode("utf-8"), capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode().strip(), 'my status: {"rate_limits": {"fi')  # output unchanged / 輸出不變
        self.assertEqual(json.loads((state / "statusline-last.json").read_text(encoding="utf-8"))["rate_limits"]
                         ["five_hour"]["resets_at"], 123)
        self.assertEqual(inst.unwrap_statusline(wrapped), self.ORIG_SL)


class TimeZones(unittest.TestCase):
    """Message zone differs from local zone (zone loading faked, so no tzdata needed).
    訊息時區與本機不同（以假時區模擬，不需要 tzdata）。"""
    NY = dt.timezone(dt.timedelta(hours=-5))

    def local(self, y, mo, d, hh, mm):  # a UTC moment as local naive time / 某個 UTC 時刻換成本機時間
        return dt.datetime(y, mo, d, hh, mm, tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)

    def test_zone_math(self):
        orig = h.load_zone
        h.load_zone = lambda name: self.NY
        try:
            now = self.local(2026, 11, 4, 2, 0)  # = Tue 2026-11-03 21:00 in UTC-5
            self.assertEqual(h.parse_reset("resets 9:30pm (America/New_York)", now), self.local(2026, 11, 4, 2, 30))
            self.assertEqual(h.parse_reset("resets Tue 11pm (America/New_York)", now), self.local(2026, 11, 4, 4, 0))
            self.assertEqual(h.parse_reset("resets Wed 9pm (America/New_York)", now), self.local(2026, 11, 5, 2, 0))
            self.assertEqual(h.parse_reset("resets Nov 4, 1am (America/New_York)", now), self.local(2026, 11, 4, 6, 0))
        finally:
            h.load_zone = orig

    def test_zone_unavailable_means_local(self):
        orig = h.load_zone
        h.load_zone = lambda name: None
        try:
            now = dt.datetime(2026, 11, 4, 22, 0)
            self.assertEqual(h.parse_reset("resets 11:30pm (Asia/Taipei)", now), dt.datetime(2026, 11, 4, 23, 30))
        finally:
            h.load_zone = orig


class Guards(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=TMP))

    def test_min_wait_when_reset_already_passed(self):
        (self.d / "statusline-last.json").write_text(json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": int(time.time()) - 240}}}), encoding="utf-8")
        start = time.time()
        code, _ = run_hook({"session_id": "m", "error": "rate_limit",
                            "last_assistant_message": "You've hit your 5-hour limit"}, self.d)
        self.assertEqual(code, 2)
        self.assertGreaterEqual(time.time() - start, 28)  # not instant / 不會立刻叫醒

    def test_recent_wake_backs_off(self):
        r5 = int(time.time()) - 60
        (self.d / "statusline-last.json").write_text(json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": r5}}}), encoding="utf-8")
        # we woke it 2 min ago for this same reset / 2 分鐘前才為同一個重置時間叫醒過
        (self.d / "woke-r.txt").write_text(json.dumps({"at": time.time() - 120, "reset": r5}), encoding="utf-8")
        p = run_hook({"session_id": "r", "error": "rate_limit",
                      "last_assistant_message": "You've hit your 5-hour limit"}, self.d, wait=False)
        time.sleep(4)
        p.kill()
        p.wait()
        log_text = (self.d / "resume.log").read_text(encoding="utf-8")
        self.assertIn("for the same reset", log_text)
        self.assertIn("retrying at", log_text)
        self.assertEqual((self.d / "unparsed-r.txt").read_text(encoding="utf-8"), "1")

    def test_later_reset_still_trusted(self):
        # Woken for the 5-hour reset, then the weekly limit hits: its exact reset must be used.
        # 5 小時額度叫醒後緊接著撞上週額度：週額度的精確時間要照常採用。
        r7 = int(time.time()) + 2 * 86400
        (self.d / "statusline-last.json").write_text(json.dumps(
            {"rate_limits": {"seven_day": {"used_percentage": 100, "resets_at": r7}}}), encoding="utf-8")
        (self.d / "woke-w.txt").write_text(json.dumps({"at": time.time() - 120, "reset": time.time() - 200}),
                                           encoding="utf-8")
        p = run_hook({"session_id": "w", "error": "rate_limit",
                      "last_assistant_message": "You've hit your weekly limit · resets Mon 12:00am"}, self.d, wait=False)
        time.sleep(4)
        p.kill()
        p.wait()
        p.stderr.close()
        self.assertIn("reset source=status line", (self.d / "resume.log").read_text(encoding="utf-8"))

    def test_old_state_cleanup(self):
        old = self.d / "waiting-old.txt"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("x", encoding="utf-8")
        t = time.time() - 10 * 86400
        os.utime(old, (t, t))
        run_hook({"session_id": "z", "error": "overloaded", "last_assistant_message": ""}, self.d)
        self.assertTrue(old.exists())  # cleanup only runs for real limit hits / 只有真的撞上限才清
        (self.d / "statusline-last.json").write_text(json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": int(time.time()) - 80}}}), encoding="utf-8")
        run_hook({"session_id": "z", "error": "rate_limit", "last_assistant_message": "You've hit your 5-hour limit"}, self.d)
        self.assertFalse(old.exists())


class Portability(unittest.TestCase):
    BASH = shutil.which("bash")

    def test_install_on_non_cjk_code_page(self):
        cdir = Path(tempfile.mkdtemp(dir=TMP))
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cdir), "CLAUDE_AUTO_RESUME_DIR": str(cdir / "s"),
               "PYTHONIOENCODING": "cp1252"}
        r = subprocess.run([sys.executable, str(INSTALL)], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))

    @unittest.skipUnless(BASH and "system32" not in BASH.lower(), "Git Bash / bash not found")
    def test_hook_command_with_odd_path(self):
        cdir = TMP / "it's $HOME `x`"
        cdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cdir), "CLAUDE_AUTO_RESUME_DIR": str(cdir / "s")}
        r = subprocess.run([sys.executable, str(INSTALL)], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        cmd = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))["hooks"]["StopFailure"][0]["hooks"][0]["command"]
        r = subprocess.run([self.BASH, "-c", cmd], input=b'{"error":"overloaded"}', capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not a usage limit", (cdir / "s" / "resume.log").read_text(encoding="utf-8"))

    def test_selftest_leaves_settings_clean(self):
        cdir = Path(tempfile.mkdtemp(dir=TMP))
        (cdir / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cdir), "PYTHONIOENCODING": "cp1252"}
        tool = ROOT / "tools" / "rewake_selftest.py"
        r = subprocess.run([sys.executable, str(tool), "arm"], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertIn("Stop", json.loads((cdir / "settings.json").read_text(encoding="utf-8"))["hooks"])
        r = subprocess.run([sys.executable, str(tool), "disarm"], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads((cdir / "settings.json").read_text(encoding="utf-8")), {"model": "opus"})


if __name__ == "__main__":
    unittest.main()
