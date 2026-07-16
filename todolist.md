# K2S Downloader — 優化待辦清單 (todolist)

> 本檔提供給後續 session 接手用。每項含：**問題**、**位置**、**建議做法**、**狀態**。
> 技術/程式術語保留英文；敘述以繁體中文（台灣用語）為主。
> 優先序 **P0（最嚴重／會損毀資料或掛死）→ P5（文件與 DX）**。
>
> 狀態圖示：`[ ]` 未處理 / `[~]` 進行中 / `[x]` 已完成。認領時請在項目後標註 `(@session-id, 日期)`。

---

## P0 — 正確性缺陷（會造成資料損毀、掛死或誤殺 process）

- [x] **P0-1 `parse_size` 的 IEC 單位換算錯誤**（2026-07-16 完成）
  - 位置：`src/k2s_downloader/core/downloader.py`（`parse_size`）
  - 修法：`KIB/MIB/GIB/TIB` 改為 2 的次方，與既有的（同樣是二進位的）`KB/MB/GB/TB` 一致。
  - **額外發現並修復**：CLI 的 `--split-size` 預設值是字串 `"20M"`，但原本的 `units` dict 只有
    `"MB"` 沒有單獨的 `"M"`，導致 `parse_size("20M")` 丟出未被 `cli.py` 捕捉的 `KeyError`
    （`cli.py` 只 catch `ValueError`）── **也就是說只要使用者沒有明確加 `--split-size`，CLI 就會直接崩潰**。
    已補上 `K/M/G/T` 單字母別名，並把「未知單位」的例外從 `KeyError` 改為 `ValueError`。
  - 測試：`tests/test_downloader_units_and_ranges.py::TestParseSizeBinaryUnits`（含 P3-1 全部項目）。

- [x] **P0-2 `_build_ranges` 切段可能產生間隙／重疊 → 合併後檔案損毀**（2026-07-16 完成）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_build_ranges`）
  - 修法：改為累進切法（`start = 前段 end + 1`，最後一段強制 `end = total_value - 1`），
    確保所有段連續、無重疊、bytes 總和恆等於 `total_size`。
  - 測試：`tests/test_downloader_units_and_ranges.py::TestBuildRangesContiguity`（多組含質數邊界的 `total_size`/`split_count`，見 P3-2）。

- [x] **P0-3 proxy lock 的 TOCTOU 與 blocking `acquire` → 潛在死結／卡住**（2026-07-16 完成）
  - 位置：`src/k2s_downloader/core/downloader.py`（新增 `_acquire_proxy_lock`，取代 `download_chunk` 內原本的選 proxy 邏輯）
  - 修法：不再「檢查 `.locked()` 後再呼叫 blocking `.acquire()`」，改為統一用 `acquire(blocking=False)`
    的迴圈（取不到就 `time.sleep(0.02)` 後重試），並在偵測到 `stop_event` 時提前返回 `None` 中止等待。
  - 測試：`tests/test_downloader_units_and_ranges.py::TestAcquireProxyLockConcurrencySafety`
    （12 threads 搶 3 個 proxy lock 驗證互斥、cancel 後能在 1 秒內脫離等待）。

- [x] **P0-4 `working_proxy_indexes` 多執行緒無鎖寫入 → race condition**（2026-07-16 完成）
  - 位置：`src/k2s_downloader/core/downloader.py`（`__init__` 新增 `_working_proxy_lock`；
    `_acquire_proxy_lock` 讀取、`download_chunk` 內 append、`refresh_proxies` 重置皆已上鎖）
  - 測試：`tests/test_downloader_units_and_ranges.py::test_working_proxy_indexes_append_is_race_free`。

- [x] **P0-5 `generate_download_urls` 的無界迴圈 → 掛死**（2026-07-16 完成）
  - 位置：`src/k2s_downloader/core/k2s_client.py`（`generate_download_urls`）
  - 修法：captcha 重試上限 `MAX_CAPTCHA_ATTEMPTS=3`；getUrl 批次迴圈上限 `MAX_URL_BATCH_ROUNDS=3`
    （連續 N 輪 0 進展即放棄該 proxy，全部失敗丟出說明「IP/proxy 被封鎖」的 RuntimeError）；
    湊不滿 `count` 時回傳部分 URL，`Downloader.download` 會把 threads 收斂到 URL 數。
    另新增 `stop_event` 參數實作 URL 產生階段的取消（`OperationCancelled` → `DownloadCancelled`），
    並把 `stop_event.clear()` 移到 `download()` 開頭避免取消訊號被清掉。
  - 測試：`tests/test_k2s_client_blocked.py`（涵蓋 P3-3 大部分情境）。

- [x] **P0-6 library 內 `sys.exit("File not found")` 會殺掉整個 process（含 GUI）**（2026-07-16 完成）
  - 位置：`src/k2s_downloader/core/k2s_client.py`
  - 修法：改丟自訂例外 `K2SFileNotFound`，由 CLI/GUI 上層以錯誤訊息呈現。
  - 測試：`tests/test_k2s_client_blocked.py::TestFileNotFound`。

---

## P1 — 專案基礎建設與依賴正確性

- [x] **P1-1 移除未使用的 runtime 依賴 `aiohttp`**（2026-07-16 完成）
  - 已自 `pyproject.toml` 的 dependencies 移除。
  - 附帶：`src/k2s_downloader.egg-info/`（建置產物，`requires.txt` 殘留過時依賴資訊）已自版控移除，
    `.gitignore` 原本就有排除規則。

- [x] **P1-2 `pyinstaller` 不應是 runtime 依賴**（2026-07-16 完成）
  - 已移到 `[project.optional-dependencies].build`（`pip install -e ".[build]"`）。

- [x] **P1-3 缺少 test / lint 的 CI**（2026-07-16 完成）
  - 新增 `.github/workflows/ci.yml`：push（main）/ PR 觸發，Python 3.9 與 3.13 matrix，
    執行 `ruff check .` 與 `pytest -q`。
  - 同時修掉 ruff 揪出的 2 個 unused import（`cli.py` 的 `Path`、`.github/scripts/test_ai_review.py` 的 `os`），
    確保 CI 首次執行即綠燈（本機已驗證：ruff 全過、55 tests 全過）。
  - 附帶（P5-2 部分完成）：`pyproject.toml` 加入 `[tool.pytest.ini_options]`
    （`testpaths` + `pythonpath = ["src"]`，本機直接跑 `pytest` 免設 PYTHONPATH）與 `[tool.ruff]` 基本設定。

- [x] **P1-4 Readme 的「Legacy Entry Points」指向不存在的 `main.py`**（2026-07-16 完成）
  - 改為「Alternative Entry Points」段，記載實際入口 `python -m k2s_downloader` 與 `k2s_gui_entry.py`，
    並新增「Development」段說明 dev 安裝、pytest、ruff 與 CI。

---

## P2 — 錯誤處理與健壯性

- [ ] **P2-1 `contextlib.suppress(Exception)` 靜默吞掉所有網路錯誤**
  - 位置：`src/k2s_downloader/core/downloader.py:391`
  - 問題：整段 GET/streaming 被吞例外，失敗原因（連線錯誤 vs. 逾時 vs. proxy 拒絕）無從診斷，只能靠事後 byte 數不符推測。
  - 建議：改捕捉特定 `requests` 例外並記錄原因到 `range_meta["last_error"]` / log。

- [ ] **P2-2 散落各處的硬編碼 timeout**
  - 位置：`downloader.py:397`（20s）、`k2s_client.py:132`（5s）、多處 `DEFAULT_TIMEOUT`
  - 建議：集中成具名常數或可由 `Downloader`/CLI 參數調整。

- [ ] **P2-3 proxy 安全性：以 `http://` 承載 HTTPS 且清單來自不可信第三方**
  - 位置：`core/proxy.py:67`（proxyscrape 來源）、`downloader.py:384`
  - 問題：使用來路不明的公開 proxy 轉發流量有 MITM 風險。
  - 建議：文件明確警告、預設 opt-in；評估直連優先、proxy 為 fallback。

- [ ] **P2-4 快取檔寫在 CWD**
  - 位置：`downloader.py`（`urls.json`）、`proxy.py:43`（`proxies.txt`）
  - 建議：改為可設定路徑或使用者資料目錄，避免污染執行目錄。

---

## P3 — 測試覆蓋

- [x] **P3-1 `parse_size` 邊界測試**（2026-07-16 完成，隨 P0-1 一併補上）
  - 涵蓋 `B/KB/MB/GB`、`KiB/MiB/GiB`、單字母 `K/M/G/T`、CLI 預設值 `"20M"`、無單位、非法輸入。
- [x] **P3-2 `_build_ranges` 連續性測試**（2026-07-16 完成，隨 P0-2 一併補上）
  - 多組 `total_size` × `split_count`（含質數邊界）：驗證各段連續、無重疊、bytes 總和等於 total。
- [x] **P3-3 `generate_download_urls` captcha / 重試分支測試**（2026-07-16 完成，隨 P0-5/P0-6 一併補上）
  - 見 `tests/test_k2s_client_blocked.py`：invalid captcha 上限、File not found、
    全部 getUrl 失敗、部分成功回傳 partial、stop_event 取消、threads 收斂。
- [ ] **P3-4 `proxy.get_working_proxies` 測試**
  - cached / refresh / recheck_cached 三條路徑與空清單 fallback。
  - 註：目前本機環境未安裝 `pytest`（`pip install -e .[dev]`），CI 亦缺，請一併處理（見 P1-3）。

---

## P4 — 程式碼品質與可維護性

- [ ] **P4-1 `main_window.py` 重複賦值與註解語言混用**
  - 位置：`gui/main_window.py:48`–`49`（`_collapsed_height` 重複）、`:298`/`:300`（`sizeHint` 重複）、`:339`/`:352`（簡繁混雜註解）
  - 建議：清理重複行、統一註解語言。
- [ ] **P4-2 `_download_once` 過長且職責混雜**
  - 位置：`downloader.py:296`
  - 建議：拆出「排程 / 單段下載 / 合併」等函式；導入 `logging` 取代零散 print/callback。
- [ ] **P4-3 `human_readable_bytes` 單位標示不一致**
  - 位置：`downloader.py:100`（除以 1024 卻標 KB/MB）
  - 建議：標示改 KiB/MiB，或改用 1000 進位。

---

## P5 — 文件與開發體驗（DX）

- [ ] **P5-1 建立 canonical 文件**（依全域規範 `requirements-en.md` 含 AC、`readme-en.md` 架構）
  - 中英雙語同步：`*-en.md`（AI/canonical）＋ `*-zh.md`（human-facing）。
- [~] **P5-2 補齊 tooling 設定**：~~`pyproject.toml` 加入 `[tool.ruff]` / `[tool.pytest.ini_options]`~~（已隨 P1-3 完成）；尚餘：新增 `CONTRIBUTING`。
- [ ] **P5-3 Readme 補充**：proxy/captcha 實際行為、法律與使用聲明、`.[dev]` 安裝與測試說明。

---

## 建議處理順序

1. ~~先做 **P0-1、P0-2** 並同步補 **P3-1、P3-2**（純函式、好測、風險高）。~~ ✅ 已完成
2. ~~再處理併發相關 **P0-3、P0-4** 與掛死相關 **P0-5、P0-6**。~~ ✅ 已完成 — **P0 全部 6 項皆已修復**
3. ~~接著 **P1**（依賴／CI／文件入口）讓專案可持續驗證。~~ ✅ 已完成（P1-1 ~ P1-4 全數完成）
4. 之後依 P2 → P4 → P5 逐步推進。← 下一步建議從 **P2**（錯誤處理與健壯性）開始

> 註：P0 全項（P0-1 ~ P0-6）與對應測試（P3-1、P3-2、P3-3）已於 2026-07-16 完成並合併於
> commit 內；分支 `fix/blocked-ip-error-handling` 已推送。所有測試（本機 `.venv`，
> `PYTHONPATH=src pytest tests`）共 55 個全數通過。接手 session 請從 **P1** 開始逐項認領。
