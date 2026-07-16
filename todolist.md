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

- [x] **P2-1 `contextlib.suppress(Exception)` 靜默吞掉所有網路錯誤**（2026-07-17 完成）
  - 位置：`src/k2s_downloader/core/downloader.py`（`download_chunk` 內部）
  - 問題：整段 GET/streaming 被吞例外，失敗原因（連線錯誤 vs. 逾時 vs. proxy 拒絕）無從診斷，只能靠事後 byte 數不符推測。
  - 修法：拆成兩層 — 內層 `except requests.exceptions.RequestException` 捕捉網路層錯誤，訊息含 proxy 標籤與原始例外，經 `_mark_chunk_failed` 寫入 `range_meta["last_error"]`／log（訊息前綴 `"request error via proxy ..."`）；
    外層新增 `except Exception`（涵蓋請求之後的非預期錯誤，如寫檔失敗），訊息前綴 `"unexpected error via proxy ..."` — 兩者都不再靜默吞掉，且維持原本「例外絕不逃出 thread、導致 range 卡在 `inUse=True` 永久等待」的安全網。移除已無用的 `contextlib` import。
  - 測試：`tests/test_downloader_error_handling.py`（`ConnectionError` 正確被記錄為 `request error`、非 `RequestException` 的例外被外層攔截記錄為 `unexpected error`，兩者皆不再落入誤導性的 `size mismatch` 訊息）。

- [x] **P2-2 散落各處的硬編碼 timeout**（2026-07-17 完成）
  - 位置：`downloader.py`（chunk request 20s、stall watchdog 20s）、`k2s_client.py`（captcha 迴圈內 5s）、`proxy.py`（proxyscrape fetch 30s）
  - 修法：全部改為具名常數 —
    `downloader.py`：`CHUNK_REQUEST_TIMEOUT = 20`（`requests.get` 的 connect/read timeout）與 `CHUNK_STALL_TIMEOUT = 20`（串流無新資料時的 watchdog，語意不同但目前同值，各自獨立命名方便未來調整）；
    `k2s_client.py`：`CAPTCHA_SOLVE_TIMEOUT = 5`（比 `DEFAULT_TIMEOUT` 短，避免單一失效 proxy 卡住整個 captcha 迴圈）；
    `proxy.py`：`PROXYSCRAPE_FETCH_TIMEOUT = 30`。
  - **評估「是否開放 CLI/Downloader 參數調整」的結論**：暫不開放。這些常數已經是模組層級可直接修改的唯一真相來源（本身就是本項的核心價值），但要再往上開放成 CLI flag 或 `Downloader.__init__` 參數會擴大 CLI 介面與建構子簽章而目前沒有實際需求（YAGNI）；若未來有使用者反應網路環境需要不同 timeout，再評估開放，記在此處供後續 session 參考。
  - 測試：`tests/test_downloader_error_handling.py` 內的 regression guard（斷言 `requests.get` 呼叫時 `timeout == downloader_module.CHUNK_REQUEST_TIMEOUT`），確保未來不會被重新內聯成 magic number。

- [x] **P2-3 proxy 安全性：以 `http://` 承載 HTTPS 且清單來自不可信第三方**（2026-07-17 完成）
  - 位置：`core/proxy.py`（proxyscrape 來源）、`downloader.py`（`_acquire_proxy_lock`、`download_chunk` 內建構 `prox` dict 處）
  - 問題：使用來路不明的公開 proxy 轉發流量有 MITM 風險。
  - 修法：
    1. **直連優先**：`_acquire_proxy_lock` 每次搶鎖前，先嘗試 index 0（`get_working_proxies` 保證恆為 `None`＝直連）；只有直連目前忙碌／不可用時才 fallback 到已知可用清單或隨機 proxy。
    2. **文件明確警告**：`Readme.md` 新增「Security Note: Public Proxies」段落；`proxy.py` 的 `get_working_proxies` docstring 與 `downloader.py` 建構 `prox` dict 處都補上 MITM 風險說明（proxy 連線本身是未驗證的明文 HTTP，即使目標是 HTTPS）。
    3. 「預設 opt-in」現況：`Downloader` 本來就是「沒有 proxy 清單就先嘗試直連，proxy 只在直連失敗/被擋時才會被用到」（`_acquire_proxy_lock` 的行為），已符合「直連優先、proxy 為 fallback」精神，故未額外變更預設行為（仍會自動 `refresh_proxies()` 取得清單以便直連失敗時可退避使用）。
  - 測試：`tests/test_proxy_preference_and_cache.py::TestDirectConnectionPreference`（直連可用時必回傳 index 0；直連忙碌時會 fallback 且不是 0）。

- [x] **P2-4 快取檔寫在 CWD**（2026-07-17 完成）
  - 位置：`downloader.py`（`urls.json`，本來就可透過 `url_cache_path` 建構參數設定）、`proxy.py`（`proxies.txt`，原本完全寫死無法覆寫）
  - 修法：`get_working_proxies` 新增 `cache_path` 參數（預設仍是 CWD 的 `"proxies.txt"`，維持向後相容），寫入前補上 `cache_path.parent.mkdir(parents=True, exist_ok=True)`，讓指定使用者資料目錄等尚未存在的巢狀路徑也能正常寫入。
    `Downloader.__init__` 新增對應的 `proxy_cache_path` 參數，並在 `refresh_proxies()` 透傳給 `get_working_proxies(cache_path=...)`。
  - 測試：`tests/test_proxy_preference_and_cache.py::TestProxyCachePathConfigurable`（自訂路徑讀取既有快取、成功寫入自訂路徑、巢狀路徑自動建立父目錄）、
    `::TestDownloaderProxyCachePathPassthrough`（`Downloader(proxy_cache_path=...)` 正確透傳、預設值與 `get_working_proxies` 預設一致）。

---

## P3 — 測試覆蓋

- [x] **P3-1 `parse_size` 邊界測試**（2026-07-16 完成，隨 P0-1 一併補上）
  - 涵蓋 `B/KB/MB/GB`、`KiB/MiB/GiB`、單字母 `K/M/G/T`、CLI 預設值 `"20M"`、無單位、非法輸入。
- [x] **P3-2 `_build_ranges` 連續性測試**（2026-07-16 完成，隨 P0-2 一併補上）
  - 多組 `total_size` × `split_count`（含質數邊界）：驗證各段連續、無重疊、bytes 總和等於 total。
- [x] **P3-3 `generate_download_urls` captcha / 重試分支測試**（2026-07-16 完成，隨 P0-5/P0-6 一併補上）
  - 見 `tests/test_k2s_client_blocked.py`：invalid captcha 上限、File not found、
    全部 getUrl 失敗、部分成功回傳 partial、stop_event 取消、threads 收斂。
- [x] **P3-4 `proxy.get_working_proxies` 測試**（2026-07-17 完成，隨 P2-4 一併補上）
  - 見 `tests/test_proxy_preference_and_cache.py::TestProxyCachePathConfigurable`：
    cached（早退路徑，讀取既有快取不驗證）、refresh（成功寫入 + 空清單 fallback 並自動建立巢狀父目錄）、
    recheck_cached（revalidate 既有清單、剔除失效 proxy）三條路徑皆涵蓋。

---

## P4 — 程式碼品質與可維護性

- [x] **P4-1 `main_window.py` 重複賦值與註解語言混用**（2026-07-17 完成）
  - 位置：`gui/main_window.py:48`–`49`（`_collapsed_height` 重複）、`:298`/`:300`（`sizeHint` 重複）、`:339`/`:352`（簡繁混雜註解）
  - 修法：刪除兩處重複賦值；`_toggle_dev_panel` 內的中文註解改成英文，統一符合專案其他檔案（`downloader.py`／`k2s_client.py`／`proxy.py`）的英文註解慣例。
  - 驗證：`ruff check` 全過、`py_compile` 語法確認（本機無 PySide6，無法完整 import 執行，但純語法／靜態層級已驗證無誤）。GUI 無測試覆蓋屬既有慣例（見 NFR-4 / `readme-en.md`）。

- [x] **P4-2 `_download_once` 過長且職責混雜**（2026-07-17 完成）
  - 位置：`downloader.py`（原 `_download_once`，約 280 行）
  - 修法：拆成 5 個獨立 method —
    `_fetch_total_size`（HEAD 請求＋Content-Length 解析）、
    `_report_progress`（進度回報，`progress_bar` 隨 `_DownloadContext` 明確傳遞而非 closure 捕捉）、
    `_download_chunk`（原本的 `download_chunk` nested closure，改為正式 method；依 PR #7 review 建議，
    共用參數封裝成 frozen dataclass `_DownloadContext` 傳遞，避免 9 參數的過長簽名）、
    `_run_scheduling_loop`（排程／派工迴圈，回傳 `failed_chunk`）、
    `_merge_parts`（合併 part 檔案為最終檔案）。
    `_download_once` 現在只負責串接這幾個 method，本身縮到約 40 行。
  - **重要正確性分析**：原本 `download_chunk` 透過 `nonlocal stop` 在多個 thread 間共用一個 boolean 來鏡射「是否已取消」。逐一檢查每個設定 `stop=True` 的路徑後，證實這個 boolean 在所有情況下都只在 `self.stop_event.is_set()` 已經（或同一敘述中正在）成立時才會被設為 True，因此完全等價於直接檢查 `self.stop_event`，且是多餘、額外增加跨 thread 共享可變狀態風險的設計。重構後 `_download_chunk` 完全不再碰任何 stop 旗標；`_run_scheduling_loop` 保留一個純本地（不跨 thread 共享）的 `stop_scheduling` 變數，只用來額外涵蓋「chunk 永久失敗」這個與 `stop_event` 無關的排程迴圈跳出條件。最終判斷「是否該丟出 `DownloadCancelled`」的地方，從讀取 `stop` 變數改為直接讀取 `self.stop_event.is_set()`，行為完全等價。
  - **「導入 logging 取代零散 print/callback」評估結論**：不採用。全專案 `print()` 呼叫只有 2 處（`cli.py` 的使用者可見結束訊息、`gui/app.py` 載入 stylesheet 失敗時的 fallback），且 `core/` 早已透過建構子 callback（`status_callback`／`progress_callback`／`proxy_state_callback`）把狀態回報跟顯示邏輯解耦 — 這正是讓同一套下載邏輯能同時驅動 CLI（`status_callback=print`）與 Qt GUI（callback 接到 Qt signal）而不需要知道對方是誰的設計。這已經是正確的抽象，改用 `logging` 反而會是不必要的架構變動，故不執行；已記錄在 `CONTRIBUTING.md` 的 code style 段落供後續參考。
  - 測試：既有 74 個測試（含 P0~P2 的並發測試）重構後原封不動全數通過，且連續跑 5 次確認無 flaky；額外用 `py_compile`／`ruff`／CLI `--help` smoke test 驗證。未新增測試檔案 — 這是純重構（behavior-preserving refactor），既有測試已經是最適合的迴歸防護網（若新增針對 private method 拆分的測試，反而會鎖死實作細節、不利未來再次重構）。

- [x] **P4-3 `human_readable_bytes` 單位標示不一致**（2026-07-17 完成）
  - 位置：`downloader.py`（`human_readable_bytes`）
  - 修法：單位標示從 `KB/MB/GB/TB` 改成 `KiB/MiB/GiB/TiB/PiB`，符合實際除以 1024 的二進位換算（並與 `gui/main_window.py` 的 `_format_speed` 既有用法一致）。
  - 測試：`tests/test_human_readable_bytes.py`（各單位邊界值、確認輸出不再出現舊的十進位風格標籤）。

---

## P5 — 文件與開發體驗（DX）

- [x] **P5-1 建立 canonical 文件**（2026-07-17 完成）
  - 新增 [`requirements-en.md`](requirements-en.md)（10 個 REQ、每個附 AC，涵蓋 URL 驗證、檔名/大小查詢、切段下載、
    重試/backoff、captcha、proxy pool、取消、媒體檢查、CLI、GUI）與 [`readme-en.md`](readme-en.md)（模組地圖、
    控制流程、threading/並發模型含關鍵設計決策說明、proxy 設計、錯誤分類、timeout 一覽表、GUI 整合、測試結構、CI）。
    所有內容逐項對照現有程式碼核實，非憑空撰寫。
  - 對應繁中人類可讀版本 [`requirements-zh.md`](requirements-zh.md)、[`readme-zh.md`](readme-zh.md) 已同步建立，
    技術術語保留英文，符合全域文件規範（`*-en.md` = AI/canonical，`*-zh.md` = human-facing）。
- [x] **P5-2 補齊 tooling 設定**（2026-07-17 完成）：~~`pyproject.toml` 加入 `[tool.ruff]` / `[tool.pytest.ini_options]`~~（已隨 P1-3 完成）；
  新增 [`CONTRIBUTING.md`](CONTRIBUTING.md)（環境設定、測試/lint 指令、程式風格慣例、commit message 格式、PR 慣例），
  `Readme.md` 的 Development 段落已加上連結。
- [x] **P5-3 Readme 補充**（2026-07-17 完成）：~~proxy 安全性警告~~（已隨 P2-3 完成）；
  新增「Captcha Handling」段落說明 CLI/GUI 的實際互動流程與答錯上限行為、「Legal Notice」段落說明本工具與 Keep2Share
  無關聯、使用者須自行負責遵守其 Terms of Service 與著作權法規。（`.[dev]` 安裝與測試說明已隨 P1-3/P1-4 完成。）

---

## 建議處理順序

1. ~~先做 **P0-1、P0-2** 並同步補 **P3-1、P3-2**（純函式、好測、風險高）。~~ ✅ 已完成
2. ~~再處理併發相關 **P0-3、P0-4** 與掛死相關 **P0-5、P0-6**。~~ ✅ 已完成 — **P0 全部 6 項皆已修復**
3. ~~接著 **P1**（依賴／CI／文件入口）讓專案可持續驗證。~~ ✅ 已完成（P1-1 ~ P1-4 全數完成）
4. ~~之後 **P2**（錯誤處理與健壯性）。~~ ✅ 已完成 — **P2 全部 4 項（P2-1 ~ P2-4）皆已修復，含 P3-4**
5. ~~接著 **P4 → P5** 逐步推進。~~ ✅ 已完成 — **P4 全部 3 項（P4-1 ~ P4-3）、P5 全部 3 項（P5-1 ~ P5-3）皆已完成**

> 註：P0（P0-1 ~ P0-6）、P1（P1-1 ~ P1-4）已於 2026-07-16 完成並合併進 `main`（PR #3，merge commit `8581b77`）。
> P2（P2-1 ~ P2-4，含補齊的 P3-4）已於 2026-07-17 完成並合併（PR #6，merge commit `71eb5f5`）。
> P4（P4-1 ~ P4-3）、P5（P5-1 ~ P5-3）已於 2026-07-17 完成，分支 `feature/p4-p5-quality-and-docs`。
> 所有測試（本機 `.venv`，`pytest -q`，`pyproject.toml` 已設 `pythonpath=["src"]` 免手動設環境變數）
> 共 74 個全數通過（連續跑 5 次確認無 flaky），`ruff check .` 全過。
>
> **todolist 的 P0 ~ P5 六個優先級目前皆已全數完成。** 若要繼續優化本專案，建議之後從頭重新檢視現況
> （程式碼可能已有新變動）並開一輪新的優先序評估，而非假設此份 todolist 仍完整反映現狀。
