# Claude Code 額度用完後自動接續（支援 Remote Control）

[English](README.md) | **繁體中文**

互動式 Claude Code session 撞到訂閱額度上限時，這支 hook 會等到額度重置，再把 Claude 叫醒，從中斷的地方接著做。
它在 **Remote Control session** 也有效，而 Claude Code 內建的自動繼續在這種 session 不會啟動。

> **狀態：實驗中。** 各個環節都已驗證過（見[驗證狀態](#驗證狀態)），但還沒有實際觀察到一次「真的用完額度、從頭到尾」的完整流程。歡迎試用並回報結果。

## 為什麼需要

Claude Code 本身可以等額度重置後自動繼續，但[官方文件](https://code.claude.com/docs/en/interactive-mode.md#wait-for-a-usage-limit-to-reset)列出幾種不會啟動的情況，其中一種就是 **Remote Control session**。

如果你為了能用手機操作而開啟 `remoteControlAtStartup`，每個 session 都會是 Remote Control session。額度重置後，除非有人手動執行 `/rate-limit-options`，否則不會自動繼續。

## 運作方式

```
回合因 "rate_limit" 錯誤失敗
  └─ Claude Code 在背景啟動 StopFailure hook（asyncRewake）
       ├─ 重置時間：狀態列快照（精確時間戳記）→ 取不到才解析錯誤訊息裡的 "resets 3:45pm"
       ├─ 睡到「重置時間 + 90 秒」，至少 30 秒（每 30 秒看一次時鐘，筆電睡眠也沒關係）
       ├─ 若對話已經有人接手（你自己，或內建的自動繼續），就不叫醒
       ├─ 若同一個 session 有更新的等待接手，就讓位
       ├─ 叫醒後 15 分鐘內又撞上限？不再相信重置時間，改用退避（30 分、1 小時…最長 8 小時）
       └─ 把提醒寫到 stderr 並 exit 2 → Claude Code 開始新的一輪，閒置時也會
```

- **精確重置時間**：Claude Code 會把 `rate_limits.five_hour.resets_at`、`seven_day.resets_at`（epoch 秒）傳給狀態列，「快到上限」的預警用的也是這份資料。安裝程式會在你原本的狀態列指令前面加一小段，把這份 JSON 存成檔案，狀態列的顯示內容不變。沒有狀態列的話，hook 會改用錯誤訊息裡的時間。
- **5 小時或週額度**：依訊息字樣判斷（"session"、"5-hour" 或 "weekly"）。距離重置超過 7.5 天的週額度不會等。
- **成本**：沒撞到上限時完全不執行。等待期間只有一個約 17 MB 的 Python 程式在睡眠，不占 Claude 的 context。叫醒時 Claude 只會收到一段簡短提醒。

## 需求

- Claude Code 需支援 `StopFailure` hook 事件與 `asyncRewake`（已在 **2.1.316** 測試）。
- 以 claude.ai 訂閱（Pro、Max 等）登入。用 API key 計費的不會有額度重置。
- Python 3.8 以上（只用標準函式庫）。
- 狀態列快照需要一個由 bash 執行的指令型狀態列（Windows 上 Claude Code 使用 Git Bash）。
- Claude Code 必須保持開著；關掉終端機，等待也會跟著結束。

## 安裝

```bash
git clone https://github.com/mysunildc/claude-code-rc-auto-resume.git
cd claude-code-rc-auto-resume
python install.py --dry-run   # 預覽 settings.json 會變成什麼，不寫入
python install.py             # 安裝（之後要更新就再跑一次）
```

安裝程式會做這些事：

1. 把 `hooks/usage_limit_resume.py` 複製到 `~/.claude/hooks/`（有設 `CLAUDE_CONFIG_DIR` 就用它）。
2. 把 `~/.claude/settings.json` 備份成 `settings.json.bak-<時間>`。
3. 加入下面這段設定，其他 hook 不會動：
   ```json
   "StopFailure": [{
     "matcher": "rate_limit",
     "hooks": [{ "type": "command",
                 "command": "\"/path/to/python\" \"~/.claude/hooks/usage_limit_resume.py\"",
                 "asyncRewake": true, "timeout": 691200 }]
   }]
   ```
4. 包裝你原本的指令型狀態列；加上 `--no-statusline` 就不包裝。

裝完後開一次 `/hooks` 或重開 Claude Code，確保新 hook 已載入。

### 給閱讀本文件的 Claude Code

如果使用者請你安裝這個工具：

1. Clone 這個 repo，執行 `python install.py --dry-run`，把結果中的 `settings.json` 給使用者看過，再執行 `python install.py`。請用 Python 3.8 以上執行，安裝程式會把這個 Python 的絕對路徑寫進 hook 指令。
2. 確認 `settings.json` 能正常解析，且 `hooks.StopFailure[*].hooks[*].command` 結尾是 `usage_limit_resume.py"`，並有 `"asyncRewake": true`。
3. 選用，證明這台電腦與這個版本能被叫醒：執行 `python tools/rewake_selftest.py arm` 後結束這一輪。大約 20 秒後，你應該會收到一段以 `REWAKE SELFTEST OK` 開頭的 hook 訊息。測試用 hook 觸發後會自動移除；如果一直沒觸發，執行 `python tools/rewake_selftest.py disarm`。
4. 不要拿使用者真正的設定跑測試；測試只會用暫存目錄。

## 驗證與排查

- **自我測試**（不必用完額度）：執行 `python tools/rewake_selftest.py arm`，結束這一輪，等約 20 秒。在 Remote Control session 裡跑，就能驗證 Remote Control 的情況。
- **自動化測試**：`python -m unittest discover -s tests -v`（約 4 到 5 分鐘，只用暫存目錄）。
- **Log**：Windows 在 `%LOCALAPPDATA%\ClaudeUsageResume\resume.log`，其他系統在 `~/.local/state/claude-usage-resume/resume.log`（有設 `$XDG_STATE_HOME` 就用它，`CLAUDE_AUTO_RESUME_DIR` 可覆寫）。真的撞到上限後應該會看到：
  ```
  ... INFO [1a2b3c4d] usage limit hit: "You've hit your session limit · resets 3:45pm"; reset source=status line; will wake at ...
  ... INFO [1a2b3c4d] woke Claude to continue
  ```

## 移除

```bash
python install.py --uninstall
```

會移除 hook 設定、還原狀態列指令，並刪除已安裝的 hook 檔。Log 會保留。

## 驗證狀態

| 項目 | 結果 | 方式 |
|---|---|---|
| 訂閱額度上限的 429 會觸發 `StopFailure`，`error` 為 `"rate_limit"`，並帶有上限訊息文字 | 已確認 | 閱讀 Claude Code 2.1.316 程式碼 |
| `StopFailure` 搭配 `asyncRewake`，exit 2 後會開始新的一輪 | 已確認 | 用 `claude -p` 刻意製造失敗實測，2.1.316 |
| `asyncRewake` 能叫醒**閒置中的 Remote Control** session | 已確認 | 在 Remote Control session 執行 `tools/rewake_selftest.py`，Windows 10，2.1.316 |
| 狀態列收得到 `rate_limits.*.resets_at` | 已確認 | 擷取真實的狀態列輸入 |
| 重置時間解析（含時區）、去重、競態、防連續叫醒、安裝與移除、特殊路徑、非中日韓語系 | 26 項自動化測試通過 | `tests/test_all.py`（Windows 10、Python 3.14） |
| **真的用完額度、從頭到尾跑一次** | **尚未觀察到** | 歡迎回報你的 `resume.log` |
| macOS、Linux | 尚未測試 | 程式路徑是跨平台寫法，歡迎回報 |

## 限制

- 這不是官方功能；之後的 Claude Code 版本可能改變 `StopFailure` 或 `asyncRewake` 的行為。
- 重置時間到時如果電腦在睡眠，會在電腦醒來後不久叫醒 Claude。
- 叫醒提醒是英文，Claude 仍會用你設定的語言回覆。
- 它不會幫你改用 API key 或付費的 usage credits。
- 快照檔存的是狀態列 JSON（model、工作目錄、session id、用量百分比），只存在本機。

## 授權

[MIT](LICENSE)
