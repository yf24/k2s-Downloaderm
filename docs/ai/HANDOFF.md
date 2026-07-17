# K2S Downloader — Handoff

> 最新進度快照，寫給接手的下一個 agent（人類也可讀）。**動態內容，很快會過期** — 若本檔與 `git log`／GitHub 上實際的 PR/Issue 狀態衝突，一律以 `git log`／GitHub 現況為準。

## 現況（2026-07-18）

- **R2-16（`ChunkDownloadFailed` 後自動重跑整個下載）2026-07-18 立項並完成**：使用者用打包出的 exe 實測時，某個 chunk 8 次重試全部透過壞掉的 proxy 逾時，整個下載直接失敗，得手動重新點「Start download」。`download()` 拆成外層（新增 `MAX_DOWNLOAD_RETRIES=3`，重試整次下載）＋內層（既有的媒體檢查重試）兩層迴圈；`_generate_urls_for_attempt()` 抽出後外層每次重跑會重新解一次 captcha、拿新 URL，重跑時 `_prepare_resume` 自動跳過已完成區段，只補真正卡住的部分。只有 `ChunkDownloadFailed` 會觸發，取消與其他例外不會。細節見 `docs/ai/todolist.md` R2-16 項。
- **R2-10（proxy pool 品質改善）2026-07-18 完成第 1~4 點**（第 5 點使用者自備清單延後、第 6 點非 proxy 替代方案評估過不採用）：`proxy.py` 改多來源並行抓取（proxyscrape v2 + 3 個 GitHub raw 清單）、驗證目標從 `api.myip.com` 改對 `k2s.cc` 發 HEAD 且檢查 status code、`proxies.txt` 快取加 12 小時 TTL；`downloader.py` 新增連續失敗自動降級機制（`_note_proxy_failure`，閾值 3 次，direct 連線豁免）。本 session 在真實網路環境下實測過：4 個來源皆可連線、對 300 個候選跑一次真實 refresh 有 47 個通過 k2s.cc 驗證。細節見 `docs/ai/todolist.md` R2-10 項。**起因**：使用者實際用打包出的 exe 下載時貼出真實 log，內含大量 proxy timeout/連線被重置/502/503，直接印證了這個項目的必要性。
- **`_mark_chunk_failed` 訊息截斷長 query string（附帶修正，非正式編號）2026-07-17 完成並 merge**（[PR #18](https://github.com/yf24/k2s-Downloaderm/pull/18)）：chunk 失敗訊息會把完整的 K2S 簽名下載 URL（`temp_url_sig` 等）印進 log，40 字元以上的 query string 現在會被截斷成 `?<truncated>`，避免洗版與意外洩漏有時效性的簽名連結。
- **R2-9（PyInstaller Windows exe 打包整備）2026-07-17 完成**（本 session 在 Windows 環境下進行，已解除先前「需要 Windows 才能驗證，暫緩」的狀態）：新增 `output_dir` 參數（`Downloader.download()`）＋ `gui/paths.py`（`QStandardPaths` 解析 app-data／Downloads 目錄）＋ GUI「Save to」資料夾選擇器＋ `k2s_gui.spec`；本機安裝 `PySide6`/`pyinstaller` 到專案 `.venv` 後實際跑過 `pyinstaller k2s_gui.spec` 建置、確認資源檔落在 `_internal/` 下、啟動建置出的 `K2SDownloaderm.exe` 確認 process 常駐後 kill 收尾。細節見 `docs/ai/todolist.md` R2-9 項。尚未做：GitHub Actions 的 Windows packaging CI job（現有 `ci.yml` 只有 ubuntu runner）。
- todolist（`docs/ai/todolist.md`）第一輪 P0~P5 全數完成並已**封存**至 [`docs/ai/todolist-archive/round-1-p0-p5.md`](todolist-archive/round-1-p0-p5.md)（R2-15 的成果，見下方）；第二輪 R2-1 ~ R2-15 的現況見該檔「第二輪（R2）」段落。
- **R2-P0（R2-1 ~ R2-3 三個並發 race）已於 2026-07-17 修正並 merge**（[PR #11](https://github.com/yf24/k2s-Downloaderm/pull/11)，merge commit `7284460`）。
- **R2-4 + R2-5（Windows 檔名 sanitize、part 檔路徑扁平化）已於 2026-07-17 修正並 merge**（[PR #12](https://github.com/yf24/k2s-Downloaderm/pull/12)，merge commit `7c6dc04`）。
- **R2-7 + R2-13（串流寫入磁碟＋斷點續傳，使用者明確需求）已於 2026-07-17 完成並 merge**（[PR #13](https://github.com/yf24/k2s-Downloaderm/pull/13)，merge commit `4da2396`）：chunk 改邊收邊寫 `.partNN.tmp`（每次 write 後 `flush()`）、確認完整位元組數後原子 `replace()` 成最終 `.partNN`；新增以磁碟為準的續傳 manifest（`<filename>.manifest.json`），gated 在 file_id/total_size/split 佈局皆相符才信任「已完成」標記。`docs/ai/requirements.md`／`docs/human/requirements.md` 已同步新增 REQ-11／NFR-6。未完成子項：GUI「開啟暫存資料夾」按鈕（需碰 `gui/main_window.py`，無自動測試覆蓋），已註記在 todolist R2-13 項下留待之後。
- **R2-14 + R2-15（review 留言截斷、todolist 歸檔機制）已於 2026-07-17 完成並 merge**（[PR #14](https://github.com/yf24/k2s-Downloaderm/pull/14)，merge commit `79278f6`）。
- **R2-6 + R2-8（HEAD status 檢查、死碼清理）已於 2026-07-17 完成並 merge**（[PR #15](https://github.com/yf24/k2s-Downloaderm/pull/15)，merge commit `ee3fff4`）：`_fetch_total_size` 非 2xx 立即丟 `RuntimeError`（不再誤讀錯誤頁的 `Content-Length`）；刪除死碼 `_load_cached_urls`。
- **R2-11（telemetry + 零成本封鎖提示）2026-07-17 部分完成**，分支 `feature/r2-11-throughput-telemetry-and-blocked-hint`：新增 `_report_progress(is_direct=...)` 與 `_maybe_report_throughput()`，每 5 秒經 `status_callback` 回報一次聚合速度＋直連/proxy 百分比＋活躍連線數；`ChunkDownloadFailed`／k2s_client 的封鎖訊息補上「動態 IP 可重啟數據機」提示。**核心的「依數據調整預設（是否讓 proxy 變 opt-in）」尚未做**——需要真實 Keep2Share 使用數據才能決定，本環境無法連線蒐集，留給下一個能實際跑真實下載的 session（或使用者自己觀察這次新增的 log 輸出後）再回來處理。測試：`tests/test_downloader_throughput_telemetry.py`（10 個）。`pytest -q` 123 passed、`ruff check .` 乾淨。PR 已 merge。R2-9、R2-10 已完成（見上方），R2-12 仍未認領。
- **[#10](https://github.com/yf24/k2s-Downloaderm/pull/10)`docs/reorganize-ai-human-audience` → `main` 已 merge**（merge commit `2226450`）。把散落在根目錄的文件依受眾（AI／人類）重新分類，新增根目錄 `AGENTS.md` 作為 AI agent 進入點。

## PR #10 具體做了什麼

（以 PR 描述本身最準，這裡只摘要）

- 新增 `docs/ai/{requirements,architecture,todolist}.md`（canonical、英文）與 `docs/human/{requirements,architecture}.md`（人類可讀、繁中），取代根目錄原本的 `requirements-en.md`／`readme-en.md`／`todolist.md`／`requirements-zh.md`／`readme-zh.md`。
- 新增 `docs/README.md`（分類索引）與根目錄 `AGENTS.md`（AI agent 進入點，含原 `.claudeprompt` 的 PR/review 三條規則）。
- 更新 `CONTRIBUTING.md`、`Readme.md` 的交叉連結。

## 已知限制／待辦（下一個 agent 可以接手的部分）

1. **Claude.ai Project 的 custom instructions 尚未更新。** 這個專案在 Claude.ai 平台端另外設定了一份「K2S Downloader — Agent Instructions」的 custom instructions（不是 repo 檔案），內容彙整自舊版文件路徑。這份設定活在平台端，這次工具鏈碰不到，需要人類手動到 Project 設定頁把它更新成指向新的 `AGENTS.md` / `docs/ai/` / `docs/human/` 路徑，或者乾脆整份刪掉、改成只留一句「詳見 repo 根目錄 `AGENTS.md`」。
2. **R2-11 的核心決策（是否讓 proxy 變 opt-in）需要真實使用數據**：這次新增的 telemetry 只是量測工具本身，實際判斷要等使用者拿這個版本跑過幾次真實下載、觀察 log 裡的「Throughput: ... direct: X%, proxy: Y%」訊息後才能做。**已有初步數據**：使用者實測過主體下載階段約 1~3MB/s（direct/proxy 比例依當下 proxy 池狀況變動），但仍未累積到足以下決策的量。
3. **R2-13 的 GUI 可見性子項未做**：暫存目錄路徑顯示＋「開啟資料夾」按鈕（見 `docs/ai/todolist.md` R2-13 項下的未完成子項說明）。
4. **R2-10 第 5 點（使用者自備 proxy 清單）未做**：CLI flag／GUI 匯入自有清單，需要碰參數解析與 GUI 檔案匯入 UI，留給有需求時再立項。
5. **R2-12（大檔 99% 尾端速度崩落對策）仍未認領**：現在有 R2-10 完成後的降級機制加持，加上使用者已提供過真實的尾端崩落現象描述，可作為驗證對策效果的基準。
6. **`.github/scripts/test_ai_review.py` 實際上不在任何 CI 步驟中執行**（`pyproject.toml` 的 `testpaths=["tests"]` 排除了 `.github/scripts/`，`ai-review.yml` workflow 也只跑腳本本身不跑 pytest）。R2-14 順手發現但未處理（範疇外）；若要修，選項包括把 `testpaths` 擴大或替 `ai-review.yml` 加一個跑這份測試的步驟，需先確認要不要把 `anthropic`/`PyGithub` 也拉進 `.[dev]` extras。

## 給下一個 agent 的建議起點

依 `AGENTS.md` 開工前建議閱讀順序：`AGENTS.md` → `docs/ai/HANDOFF.md`（本檔）→ `docs/ai/requirements.md` → `docs/ai/architecture.md` → `docs/ai/todolist.md` → `CONTRIBUTING.md`。若任務是「清理上面第 1、2 點的殘留」，先跟人類確認是否已取得可寫入 credential，再動作；純文件變動不需要跑 `pytest`/`ruff`，但若牵動 `src/`/`tests/`，記得照 `CONTRIBUTING.md` 的流程跑過再 push。
