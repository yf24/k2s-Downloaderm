# K2S Downloader — Handoff

> 最新進度快照，寫給接手的下一個 agent（人類也可讀）。**動態內容，很快會過期** — 若本檔與 `git log`／GitHub 上實際的 PR/Issue 狀態衝突，一律以 `git log`／GitHub 現況為準。

## 現況（2026-07-17）

- todolist（`docs/ai/todolist.md`）第一輪 P0~P5 全數完成；2026-07-17 完成第二輪靜態 code review，新增 R2-1 ~ R2-13 backlog，詳見該檔「第二輪（R2）」段落。
- **R2-P0（R2-1 ~ R2-3 三個並發 race）已於 2026-07-17 修正並 merge**（[PR #11](https://github.com/yf24/k2s-Downloaderm/pull/11)，merge commit `7284460`）。
- **R2-4 + R2-5（Windows 檔名 sanitize、part 檔路徑扁平化）已於 2026-07-17 修正並 merge**（[PR #12](https://github.com/yf24/k2s-Downloaderm/pull/12)，merge commit `7c6dc04`）。過程中順手修掉 `tests/test_downloader_status_code.py` 一個既有 fixture 缺口（該測試直接呼叫 `_download_once` 略過 `download()` 的 `tmp_dir.mkdir(...)`，先前靠絕對路徑覆蓋 `tmp_dir` 前綴的巧合掩蓋了這個缺口）。
- **R2-7 + R2-13（串流寫入磁碟＋斷點續傳，使用者明確需求，兩項綁定實作）已於 2026-07-17 完成**，分支 `feature/r2-7-r2-13-streaming-resume`：
  - chunk 改為邊收邊寫 `.partNN.tmp`（每次 write 後 `flush()`），確認完整位元組數後才原子 `replace()` 成最終 `.partNN`；`_merge_parts` 改 `shutil.copyfileobj` 串流合併。
  - 新增以磁碟為準的續傳 manifest（`<filename>.manifest.json`），記錄 file_id/total_size/split 佈局/各區段完成狀態；下載開始前驗證 manifest 是否與本次執行相符（相符才信任「已完成」標記，且仍逐一核對磁碟上實際 part 檔大小）——不符或缺席則清掉同檔名下的殘留 part／manifest，避免不同來源檔案因剛好同名同大小被誤接續。成功合併後刪除 manifest，取消／永久失敗則保留供下次續傳。
  - 新測試 `tests/test_downloader_resume_and_streaming.py`（9 個，對修正前程式碼 7 個 fail、2 個是本就成立的不變量）。`pytest -q` 109 passed、`ruff check .` 乾淨，連跑 3 輪無 flaky。
  - **範疇變更已同步**：`docs/ai/requirements.md`／`docs/human/requirements.md` 新增 REQ-11（斷點續傳）與 NFR-6（串流寫入有界記憶體），更新 §2 Scope 與 AC-3.5/AC-3.6 措辭。
  - 未完成子項（有意識延後）：GUI「開啟暫存資料夾」按鈕未做（需要碰 `gui/main_window.py`，無自動測試覆蓋）；目前可見性靠 `status_callback` 訊息（GUI log 面板本就顯示）與磁碟上的 manifest/part 檔達成。
  - PR 待人類 review/merge。其餘 R2-6、R2-8 ~ R2-12 仍未認領。
- **[#10](https://github.com/yf24/k2s-Downloaderm/pull/10)`docs/reorganize-ai-human-audience` → `main` 已 merge**（merge commit `2226450`）。把散落在根目錄的文件依受眾（AI／人類）重新分類，新增根目錄 `AGENTS.md` 作為 AI agent 進入點。

## PR #10 具體做了什麼

（以 PR 描述本身最準，這裡只摘要）

- 新增 `docs/ai/{requirements,architecture,todolist}.md`（canonical、英文）與 `docs/human/{requirements,architecture}.md`（人類可讀、繁中），取代根目錄原本的 `requirements-en.md`／`readme-en.md`／`todolist.md`／`requirements-zh.md`／`readme-zh.md`。
- 新增 `docs/README.md`（分類索引）與根目錄 `AGENTS.md`（AI agent 進入點，含原 `.claudeprompt` 的 PR/review 三條規則）。
- 更新 `CONTRIBUTING.md`、`Readme.md` 的交叉連結。

## 已知限制／待辦（下一個 agent 可以接手的部分）

1. **Claude.ai Project 的 custom instructions 尚未更新。** 這個專案在 Claude.ai 平台端另外設定了一份「K2S Downloader — Agent Instructions」的 custom instructions（不是 repo 檔案），內容彙整自舊版文件路徑。這份設定活在平台端，這次工具鏈碰不到，需要人類手動到 Project 設定頁把它更新成指向新的 `AGENTS.md` / `docs/ai/` / `docs/human/` 路徑，或者乾脆整份刪掉、改成只留一句「詳見 repo 根目錄 `AGENTS.md`」。
2. **R2-7 + R2-13 的 PR 尚未 review／merge**（見上方「現況」）。下一個 agent 若被要求「處理 review 意見」，依 `AGENTS.md` 第 6 節規則：讀取 PR 上的最新 AI reviewer 留言、只處理 Critical/嚴重 Improvement、忽略 nitpick、一輪對話只 push 一次，push 完就停止等人類 merge。
3. **R2-13 的 GUI 可見性子項未做**：暫存目錄路徑顯示＋「開啟資料夾」按鈕（見上方「現況」的未完成子項說明）。

## 給下一個 agent 的建議起點

依 `AGENTS.md` 開工前建議閱讀順序：`AGENTS.md` → `docs/ai/HANDOFF.md`（本檔）→ `docs/ai/requirements.md` → `docs/ai/architecture.md` → `docs/ai/todolist.md` → `CONTRIBUTING.md`。若任務是「清理上面第 1、2 點的殘留」，先跟人類確認是否已取得可寫入 credential，再動作；純文件變動不需要跑 `pytest`/`ruff`，但若牵動 `src/`/`tests/`，記得照 `CONTRIBUTING.md` 的流程跑過再 push。
