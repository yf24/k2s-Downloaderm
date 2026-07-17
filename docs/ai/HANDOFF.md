# K2S Downloader — Handoff

> 最新進度快照，寫給接手的下一個 agent（人類也可讀）。**動態內容，很快會過期** — 若本檔與 `git log`／GitHub 上實際的 PR/Issue 狀態衝突，一律以 `git log`／GitHub 現況為準。

## 現況（2026-07-17）

- todolist（`docs/ai/todolist.md`）第一輪 P0~P5 全數完成並已**封存**至 [`docs/ai/todolist-archive/round-1-p0-p5.md`](todolist-archive/round-1-p0-p5.md)（R2-15 的成果，見下方）；第二輪 R2-1 ~ R2-15 的現況見該檔「第二輪（R2）」段落。
- **R2-P0（R2-1 ~ R2-3 三個並發 race）已於 2026-07-17 修正並 merge**（[PR #11](https://github.com/yf24/k2s-Downloaderm/pull/11)，merge commit `7284460`）。
- **R2-4 + R2-5（Windows 檔名 sanitize、part 檔路徑扁平化）已於 2026-07-17 修正並 merge**（[PR #12](https://github.com/yf24/k2s-Downloaderm/pull/12)，merge commit `7c6dc04`）。
- **R2-7 + R2-13（串流寫入磁碟＋斷點續傳，使用者明確需求）已於 2026-07-17 完成並 merge**（[PR #13](https://github.com/yf24/k2s-Downloaderm/pull/13)，merge commit `4da2396`）：chunk 改邊收邊寫 `.partNN.tmp`（每次 write 後 `flush()`）、確認完整位元組數後原子 `replace()` 成最終 `.partNN`；新增以磁碟為準的續傳 manifest（`<filename>.manifest.json`），gated 在 file_id/total_size/split 佈局皆相符才信任「已完成」標記。`docs/ai/requirements.md`／`docs/human/requirements.md` 已同步新增 REQ-11／NFR-6。未完成子項：GUI「開啟暫存資料夾」按鈕（需碰 `gui/main_window.py`，無自動測試覆蓋），已註記在 todolist R2-13 項下留待之後。
- **R2-14 + R2-15（review 留言截斷、todolist 歸檔機制，使用者 2026-07-17 直接提出）已完成**，分支 `feature/r2-14-15-review-script-and-todolist-archive`：
  - R2-14：改寫 `.github/scripts/ai_review.py` 的 `build_review_prompt`（移除「請給予肯定」指示，改為明確要求不寫「整體評價」、把篇幅留給 Critical/Improvement）；新測試 `.github/scripts/test_ai_review.py::TestReviewPromptDoesNotAskForPraise`。附帶發現：這份測試檔因 `pyproject.toml` 的 `testpaths=["tests"]` 目前不在任何 CI 步驟中實際執行，本次未處理（範疇外）。
  - R2-15：新增 `docs/ai/todolist-archive/`，把已全數完成的第一輪（P0~P5）搬過去，`docs/ai/todolist.md` 從 357 行降到約 180 行；`AGENTS.md`／`CONTRIBUTING.md`／`docs/README.md` 同步補上歸檔規則說明。
  - `pytest -q` 109 passed（另 `.github/scripts/test_ai_review.py` 8 passed，需本機另裝 `anthropic`/`PyGithub` 才能跑）、`ruff check .` 乾淨。PR 待人類 review/merge。
- **[#10](https://github.com/yf24/k2s-Downloaderm/pull/10)`docs/reorganize-ai-human-audience` → `main` 已 merge**（merge commit `2226450`）。把散落在根目錄的文件依受眾（AI／人類）重新分類，新增根目錄 `AGENTS.md` 作為 AI agent 進入點。

## PR #10 具體做了什麼

（以 PR 描述本身最準，這裡只摘要）

- 新增 `docs/ai/{requirements,architecture,todolist}.md`（canonical、英文）與 `docs/human/{requirements,architecture}.md`（人類可讀、繁中），取代根目錄原本的 `requirements-en.md`／`readme-en.md`／`todolist.md`／`requirements-zh.md`／`readme-zh.md`。
- 新增 `docs/README.md`（分類索引）與根目錄 `AGENTS.md`（AI agent 進入點，含原 `.claudeprompt` 的 PR/review 三條規則）。
- 更新 `CONTRIBUTING.md`、`Readme.md` 的交叉連結。

## 已知限制／待辦（下一個 agent 可以接手的部分）

1. **Claude.ai Project 的 custom instructions 尚未更新。** 這個專案在 Claude.ai 平台端另外設定了一份「K2S Downloader — Agent Instructions」的 custom instructions（不是 repo 檔案），內容彙整自舊版文件路徑。這份設定活在平台端，這次工具鏈碰不到，需要人類手動到 Project 設定頁把它更新成指向新的 `AGENTS.md` / `docs/ai/` / `docs/human/` 路徑，或者乾脆整份刪掉、改成只留一句「詳見 repo 根目錄 `AGENTS.md`」。
2. **R2-14 + R2-15 的 PR 尚未 review／merge**（見上方「現況」）。下一個 agent 若被要求「處理 review 意見」，依 `AGENTS.md` 第 6 節規則：讀取 PR 上的最新 AI reviewer 留言、只處理 Critical/嚴重 Improvement、忽略 nitpick、一輪對話只 push 一次，push 完就停止等人類 merge。
3. **R2-13 的 GUI 可見性子項未做**：暫存目錄路徑顯示＋「開啟資料夾」按鈕（見 `docs/ai/todolist.md` R2-13 項下的未完成子項說明）。
4. **`.github/scripts/test_ai_review.py` 實際上不在任何 CI 步驟中執行**（`pyproject.toml` 的 `testpaths=["tests"]` 排除了 `.github/scripts/`，`ai-review.yml` workflow 也只跑腳本本身不跑 pytest）。R2-14 順手發現但未處理（範疇外）；若要修，選項包括把 `testpaths` 擴大或替 `ai-review.yml` 加一個跑這份測試的步驟，需先確認要不要把 `anthropic`/`PyGithub` 也拉進 `.[dev]` extras。

## 給下一個 agent 的建議起點

依 `AGENTS.md` 開工前建議閱讀順序：`AGENTS.md` → `docs/ai/HANDOFF.md`（本檔）→ `docs/ai/requirements.md` → `docs/ai/architecture.md` → `docs/ai/todolist.md` → `CONTRIBUTING.md`。若任務是「清理上面第 1、2 點的殘留」，先跟人類確認是否已取得可寫入 credential，再動作；純文件變動不需要跑 `pytest`/`ruff`，但若牵動 `src/`/`tests/`，記得照 `CONTRIBUTING.md` 的流程跑過再 push。
