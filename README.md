# Claude Code auto-resume after usage limit (works with Remote Control)

**English** | [繁體中文](README.zh-TW.md)

When an interactive Claude Code session hits your subscription usage limit, this hook waits until the
limit resets and then wakes Claude so it continues where it stopped. It also works in **Remote Control
sessions**, which Claude Code's built-in auto-continue skips.

> **Status: experimental.** The building blocks have been verified (see [Verification status](#verification-status)),
> but a complete run through a real usage limit has not been observed yet. Please try it and report back.

## Why

Claude Code can wait for a usage limit to reset and then continue automatically, but the
[official docs](https://code.claude.com/docs/en/interactive-mode.md#wait-for-a-usage-limit-to-reset) list sessions
where this does not start, including **Remote Control sessions**. If you turn on `remoteControlAtStartup` so you
can drive every session from your phone, every session becomes a Remote Control session, and nothing continues
after the reset unless someone runs `/rate-limit-options`.

## How it works

```
turn fails with error "rate_limit"
  └─ Claude Code starts the StopFailure hook in the background (asyncRewake)
       ├─ reset time: status line snapshot (exact epoch) → otherwise parse "resets 3:45pm" from the error text
       ├─ sleep until reset + 90 s, at least 30 s (checks the clock every 30 s, so laptop sleep is fine)
       ├─ skip if the conversation already continued (you, or the built-in auto-continue)
       ├─ skip if a newer wait for the same session took over
       ├─ limited again within 15 min of our wake-up? distrust reset times, back off (30 min, 1 h, … 8 h)
       └─ write a reminder to stderr and exit 2 → Claude Code starts a new turn, even when idle
```

- **Exact reset time.** Claude Code passes `rate_limits.five_hour.resets_at` / `seven_day.resets_at` (epoch
  seconds) to the status line; that's the same data behind the "approaching your limit" warning. The installer adds
  a tiny prefix to your existing status line command that saves this JSON to a file. Your status line output is
  unchanged. Without a status line, the hook parses the time from the error text instead.
- **5-hour vs weekly.** It picks the window from the message ("session"/"5-hour" vs "weekly"). Weekly resets more
  than 7.5 days away are not waited for.
- **Cost.** Nothing runs until a limit is hit. While waiting, one small Python process (~17 MB) sleeps; it adds
  nothing to Claude's context. On wake, Claude gets one short reminder.

## Requirements

- Claude Code with the `StopFailure` hook event and `asyncRewake` (tested on **2.1.316**).
- Signed in with a claude.ai subscription (Pro / Max / …). API key users have no usage-limit resets.
- Python 3.8+ (standard library only).
- For the status line snapshot: a command status line run by bash (on Windows, Claude Code uses Git Bash).
- The Claude Code process must stay open (closing the terminal ends the wait).

## Install

```bash
git clone https://github.com/mysunildc/claude-code-rc-auto-resume.git
cd claude-code-rc-auto-resume
python install.py --dry-run   # preview the settings.json change, writes nothing
python install.py             # install (re-run any time to update)
```

The installer:

1. copies `hooks/usage_limit_resume.py` to `~/.claude/hooks/` (respects `CLAUDE_CONFIG_DIR`);
2. backs up `~/.claude/settings.json` to `settings.json.bak-<timestamp>`;
3. adds this entry (other hooks are kept):
   ```json
   "StopFailure": [{
     "matcher": "rate_limit",
     "hooks": [{ "type": "command",
                 "command": "\"/path/to/python\" \"~/.claude/hooks/usage_limit_resume.py\"",
                 "asyncRewake": true, "timeout": 691200 }]
   }]
   ```
4. wraps your existing command status line, unless you pass `--no-statusline`.

Then open `/hooks` once, or restart Claude Code, to be sure the hook is loaded.

### For Claude Code agents reading this

If a user asks you to install this:

1. Clone the repo, run `python install.py --dry-run`, show the user the resulting `settings.json`, then run
   `python install.py`. Use a Python 3.8+ interpreter; the installer writes its absolute path into the hook command.
2. Confirm that `settings.json` parses and contains `hooks.StopFailure[*].hooks[*].command` ending in
   `usage_limit_resume.py"`, with `"asyncRewake": true`.
3. Optional proof that waking works on this machine and version: run `python tools/rewake_selftest.py arm`, then end
   your turn. About 20 s later you should receive a hook message starting with `REWAKE SELFTEST OK`. The test hook
   removes itself after it fires; if it never fires, run `python tools/rewake_selftest.py disarm`.
4. Do not run the test suite against the user's real settings; the tests use temporary directories only.

## Verify / troubleshoot

- **Self-test** (no usage limit needed): `python tools/rewake_selftest.py arm`, end the turn, wait about 20 s.
  Run it inside a Remote Control session to verify the Remote Control case.
- **Tests:** `python -m unittest discover -s tests -v` (about 4–5 minutes; uses temp directories only).
- **Log:** `%LOCALAPPDATA%\ClaudeUsageResume\resume.log` on Windows, `~/.local/state/claude-usage-resume/resume.log`
  elsewhere (`$XDG_STATE_HOME` respected, `CLAUDE_AUTO_RESUME_DIR` overrides). After a real limit you should see:
  ```
  ... INFO [1a2b3c4d] usage limit hit: "You've hit your session limit · resets 3:45pm"; reset source=status line; will wake at ...
  ... INFO [1a2b3c4d] woke Claude to continue
  ```

## Uninstall

```bash
python install.py --uninstall
```

Removes the hook entry, unwraps the status line, and deletes the installed hook file. Logs are kept.

## Verification status

| What | Result | How |
|---|---|---|
| A subscription usage-limit 429 fires `StopFailure` with `error: "rate_limit"` and the rendered limit text | Confirmed by reading Claude Code 2.1.316 source | static check |
| `StopFailure` + `asyncRewake` exit 2 starts a new turn | Confirmed | headless `claude -p` run with a forced failure, 2.1.316 |
| `asyncRewake` wakes an **idle Remote Control** session | Confirmed | `tools/rewake_selftest.py` in a Remote Control session, Windows 10, 2.1.316 |
| Status line receives `rate_limits.*.resets_at` | Confirmed | captured real status line input |
| Reset parsing (incl. time zones), dedup, races, loop guard, installer round-trip, odd paths, non-CJK code pages | 26 automated tests pass | `tests/test_all.py` (Windows 10, Python 3.14) |
| **Full run through a real usage limit** | **Not observed yet** | please report your `resume.log` |
| macOS / Linux | Not tested yet | code paths are portable; reports welcome |

## Limitations

- Not an official feature; a future Claude Code release could change `StopFailure` or `asyncRewake`.
- If the machine is asleep at reset time, Claude is woken shortly after it wakes up.
- The wake-up reminder is in English; Claude still replies in your configured language.
- It never switches you to an API key or paid usage credits.
- The snapshot file holds the status line JSON (model, cwd, session id, usage percentages), stored locally only.

## License

[MIT](LICENSE)
