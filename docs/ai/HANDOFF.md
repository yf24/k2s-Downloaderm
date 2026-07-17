# K2S Downloader — Handoff

> 最新進度快照，寫給接手的下一個 agent（人類也可讀）。**動態內容，很快會過期** — 若本檔與 `git log`／GitHub 上實際的 PR/Issue 狀態衝突，一律以 `git log`／GitHub 現況為準。

## 現況（2026-07-17）

- todolist（`docs/ai/todolist.md`）第一輪 P0~P5 全數完成；2026-07-17 完成第二輪靜態 code review，新增 R2-1 ~ R2-13 backlog（並發 race、Windows 相容性、exe 打包整備、proxy 來源改善、加速機制量測驗證、大檔 99% 尾端速度崩落對策、斷點續傳與暫存可見性），詳見該檔「第二輪（R2）」段落。其中 R2-7＋R2-13（串流寫入＋續傳）是使用者明確提出的需求，優先度高。
- **R2-P0（R2-1 ~ R2-3 三個並發 race）已於 2026-07-17 修正並 merge**（[PR #11](https://github.com/yf24/k2s-Downloaderm/pull/11)，`main` 現在含 commit `2ce15c6`）。
- **R2-4 + R2-5（Windows 檔名 sanitize、part 檔路徑扁平化）已於 2026-07-17 修正**，分支 `fix/r2-4-5-windows-paths`，含新測試 `tests/test_downloader_filename_and_paths.py`（22 個測試，對修正前程式碼會 import error / fail，修正後全 pass）。修正過程中發現並修掉 `tests/test_downloader_status_code.py` 一個既有 fixture 缺口（該測試直接呼叫 `_download_once` 略過 `download()` 的 `tmp_dir.mkdir(...)`，先前靠絕對路徑覆蓋 `tmp_dir` 前綴的巧合掩蓋了這個缺口；R2-5 修正後這巧合不再成立，需顯式建立 `tmp_dir`）。`pytest -q` 100 passed、`ruff check .` 乾淨。PR 待人類 review/merge。其餘 R2-6 ~ R2-13 仍未認領。
- **[#10](https://github.com/yf24/k2s-Downloaderm/pull/10)`docs/reorganize-ai-human-audience` → `main` 已 merge**（merge commit `2226450`）。把散落在根目錄的文件依受眾（AI／人類）重新分類，新增根目錄 `AGENTS.md` 作為 AI agent 進入點。
- 本檔（`docs/ai/HANDOFF.md`）本身也是同一個 PR #10 的一部分（同分支上的追加 commit）。

## PR #10 具體做了什麼

（以 PR 描述本身最準，這裡只摘要）

- 新增 `docs/ai/{requirements,architecture,todolist}.md`（canonical、英文）與 `docs/human/{requirements,architecture}.md`（人類可讀、繁中），取代根目錄原本的 `requirements-en.md`／`readme-en.md`／`todolist.md`／`requirements-zh.md`／`readme-zh.md`。
- 新增 `docs/README.md`（分類索引）與根目錄 `AGENTS.md`（AI agent 進入點，含原 `.claudeprompt` 的 PR/review 三條規則）。
- 更新 `CONTRIBUTING.md`、`Readme.md` 的交叉連結。

## 已知限制／待辦（下一個 agent 可以接手的部分）

1. ~~舊路徑 stub 檔案尚未真正刪除。~~ **已於本機清理完成（2026-07-17）**：`requirements-en.md`、`readme-en.md`、`todolist.md`、`requirements-zh.md`、`readme-zh.md`、`.claudeprompt` 這 6 個 stub 檔案已用本機 `git rm` 刪除，變更在工作目錄中，尚未 commit／push（需要人類確認後執行 `git commit` + `git push`）。
2. **Claude.ai Project 的 custom instructions 尚未更新。** 這個專案在 Claude.ai 平台端另外設定了一份「K2S Downloader — Agent Instructions」的 custom instructions（不是 repo 檔案），內容彙整自舊版文件路徑（含一份已經不存在於 repo 的 `HANDOFF.md`）。這份設定活在平台端，這次工具鏈碰不到，需要人類手動到 Project 設定頁把它更新成指向新的 `AGENTS.md` / `docs/ai/` / `docs/human/` 路徑，或者乾脆整份刪掉、改成只留一句「詳見 repo 根目錄 `AGENTS.md`」。
3. **PR #10 尚未 review／merge。** 下一個 agent 若被要求「處理 review 意見」，依 `AGENTS.md` 第 6 節規則：讀取 PR 上的最新 AI reviewer 留言、只處理 Critical/嚴重 Improvement、忽略 nitpick、一輪對話只 push 一次，push 完就停止等人類 merge。

## 給下一個 agent 的建議起點

依 `AGENTS.md` 開工前建議閱讀順序：`AGENTS.md` → `docs/ai/HANDOFF.md`（本檔）→ `docs/ai/requirements.md` → `docs/ai/architecture.md` → `docs/ai/todolist.md` → `CONTRIBUTING.md`。若任務是「清理上面第 1、2 點的殘留」，先跟人類確認是否已取得可寫入 credential，再動作；純文件變動不需要跑 `pytest`/`ruff`，但若牵動 `src/`/`tests/`，記得照 `CONTRIBUTING.md` 的流程跑過再 push。
