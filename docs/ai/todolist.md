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
  - 驗證：`ruff check` 全過、`py_compile` 語法確認（本機無 PySide6，無法完整 import 執行，但純語法／靜態層級已驗證無誤）。GUI 無測試覆蓋屬既有慣例（見 NFR-4 / `docs/ai/architecture.md`）。

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
  - 新增 [`requirements.md`](requirements.md)（10 個 REQ、每個附 AC，涵蓋 URL 驗證、檔名/大小查詢、切段下載、
    重試/backoff、captcha、proxy pool、取消、媒體檢查、CLI、GUI）與 [`architecture.md`](architecture.md)（模組地圖、
    控制流程、threading/並發模型含關鍵設計決策說明、proxy 設計、錯誤分類、timeout 一覽表、GUI 整合、測試結構、CI）。
    所有內容逐項對照現有程式碼核實，非憑空撰寫。
  - 對應繁中人類可讀版本 [`docs/human/requirements.md`](../human/requirements.md)、[`docs/human/architecture.md`](../human/architecture.md) 已同步建立，
    技術術語保留英文，符合全域文件規範（`docs/ai/` = AI/canonical，`docs/human/` = human-facing；此規範於 2026-07-17 由 P5-4 從原本以 `*-en.md`/`*-zh.md` 檔名區分改為以目錄區分）。
- [x] **P5-2 補齊 tooling 設定**（2026-07-17 完成）：~~`pyproject.toml` 加入 `[tool.ruff]` / `[tool.pytest.ini_options]`~~（已隨 P1-3 完成）；
  新增 [`CONTRIBUTING.md`](../../CONTRIBUTING.md)（環境設定、測試/lint 指令、程式風格慣例、commit message 格式、PR 慣例），
  `Readme.md` 的 Development 段落已加上連結。
- [x] **P5-3 Readme 補充**（2026-07-17 完成）：~~proxy 安全性警告~~（已隨 P2-3 完成）；
  新增「Captcha Handling」段落說明 CLI/GUI 的實際互動流程與答錯上限行為、「Legal Notice」段落說明本工具與 Keep2Share
  無關聯、使用者須自行負責遵守其 Terms of Service 與著作權法規。（`.[dev]` 安裝與測試說明已隨 P1-3/P1-4 完成。）
- [x] **P5-4 文件依受眾（AI／人類）重新分類，新增 `AGENTS.md` 統一進入點**（2026-07-17 完成）
  - 問題：`requirements-en.md`／`readme-en.md`／`requirements-zh.md`／`readme-zh.md`／`todolist.md`／`.claudeprompt` 全部散落在根目錄，AI 與人類讀者都得自己判斷該讀哪一份，容易搞混，也讓根目錄看起來雜亂。
  - 修法：canonical（AI-facing，英文）文件搬到 `docs/ai/`（`requirements.md`／`architecture.md`／`todolist.md`，也就是本檔案現在的位置），人類可讀繁中版本搬到 `docs/human/`（`requirements.md`／`architecture.md`）；新增根目錄 [`AGENTS.md`](../../AGENTS.md) 作為 AI agent 開工前的統一進入點（併入原 `.claudeprompt` 的 PR/review 規則）；新增 [`docs/README.md`](../README.md) 說明分類方式與閱讀順序。`Readme.md`／`CONTRIBUTING.md`／`LICENSE` 維持在根目錄（GitHub 慣例位置；`pyproject.toml` 的 `readme` 欄位也指向 `Readme.md`，不可搬動）。
  - **已知限制**：目前串接的 GitHub 工具（`create_or_update_file`／`push_files`）沒有刪除檔案的能力，舊路徑（`requirements-en.md`、`readme-en.md`、`requirements-zh.md`、`readme-zh.md`、`todolist.md`、`.claudeprompt`）無法真的移除，已改成內容只有一兩行「已搬移至 X」的 stub。若要徹底清乾淨根目錄，需要有人用 `git rm` 或 GitHub 網頁介面手動刪除這 6 個 stub 檔案（見對應 PR 說明）。
  - 測試：純文件變動，不影響任何程式碼／既有測試；透過 PR review 確認所有新增文件間的交叉連結路徑皆正確可用。

---

# 第二輪（R2）— 2026-07-17 靜態檢視後的新 backlog

> 背景：第一輪 P0~P5 全數完成後，依「重新從頭檢視現況」原則做了一次完整靜態 code review
> （本輪環境無法對 Keep2Share 做實際功能測試，所有發現皆來自程式碼閱讀，尚未實測重現）。
> 動機含四個檢視面向：(1) 殘留嚴重問題、(2) Windows exe 打包可行性、(3) proxy 來源替代方案、
> (4) 對照「突破瀏覽器 50KB/s 免費下載限制」這個根本目的的達成度。

## R2-P0 — 並發正確性（靜態分析發現的 race，尚未實測重現）

- [x] **R2-1 chunk 完成路徑的 `inUse`/`downloaded` 寫入順序 race → 進度重複計數、可能提前 merge**（2026-07-17 完成；測試：`tests/test_downloader_concurrency_races.py::TestCompletionPublishOrder`。實作：成功路徑改為在 `_progress_lock` 內先設 `downloaded` 再清 `inUse` 並一併遞增 `_done_count`；排程端重用分支的「檢查＋標記＋計數」同樣移入 `_progress_lock` 原子完成，且 size 相符時一律 `continue` 不再重派。）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_download_chunk` 成功路徑 `range_meta["inUse"] = False` 先於 `range_meta["downloaded"] = True`；`_run_scheduling_loop` 的「part 檔已存在」重用分支）
  - 問題：排程執行緒若在這兩行之間讀到 `inUse=False, downloaded=False`，且 part 檔已寫完（`write_bytes` 在更早），會走進重用分支再次 `_report_progress(bytes)` 並 `_done_count += 1` —— 與 chunk 執行緒自己的計數重複。後果：進度條超過 100%、`_done_count` 超前使 `while self._done_count < len(ranges)` 提前跳出，若此時仍有其他 range 未完成，`_merge_parts` 會因缺 part 檔丟出未分類的 `FileNotFoundError`（違反錯誤分類慣例）。
  - 建議做法：把成功路徑改為先設 `downloaded = True` 再設 `inUse = False`（排程端讀取順序是先 `inUse` 後 `downloaded`，交換寫入順序即可關閉這個窗口）；並補一個以 `_progress_lock` 保護「檢查＋標記」的防護。
  - 測試：模擬排程執行緒與 chunk 執行緒交錯（可將兩行寫入之間注入 hook），驗證 `_done_count` 不重複遞增。

- [x] **R2-2 size-mismatch 路徑提前釋放 `url_locks` → 可能釋放到別的 chunk 正持有的 lock**（2026-07-17 完成；測試：`tests/test_downloader_concurrency_races.py::TestUrlLockSingleRelease`。實作：刪除 size-mismatch 分支的提前釋放，統一由 `finally` 單點釋放。）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_download_chunk` 內 size-mismatch 分支的 `self.url_locks[thread_index].release()`，與 `finally` 內的釋放重複）
  - 問題：size mismatch 時先釋放一次 url lock，之後 `finally` 又用 `.locked()` 檢查再釋放一次。`threading.Lock` 沒有擁有者概念 —— 若在兩次釋放之間排程器已把同一 `thread_index` 派給新 chunk（重新 acquire），`finally` 的第二次釋放會把**新 chunk 正持有的 lock** 放掉，排程器便可能對同一條 download URL 同時派兩個 chunk（同一 token 兩條並行連線，可能觸發 host 端拒絕/限速，且違反 url lock 的設計不變量）。
  - 建議做法：刪除 size-mismatch 分支的提前釋放，統一只由 `finally` 釋放（該分支 `return` 後必然進 `finally`，提前釋放毫無必要）。
  - 測試：併發測試驗證同一 `thread_index` 不會被兩個活躍 chunk 同時使用。

- [x] **R2-3 失敗/取消後不 join in-flight chunk threads → 殘留執行緒與重試下載互相干擾**（2026-07-17 完成；測試：`tests/test_downloader_concurrency_races.py::TestSchedulingLoopJoinsChunkThreads`。實作：`_run_scheduling_loop` 追蹤 chunk `Thread` handles，`finally` 內先 join（共用 deadline `CHUNK_THREADS_JOIN_TIMEOUT=30s`）再釋放 lock；永久失敗時額外 `stop_event.set()` 讓 in-flight threads 提早退出——`_download_once` 會先丟 `ChunkDownloadFailed` 才檢查 `stop_event`，不會誤判成取消。）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_run_scheduling_loop` 以 daemon thread 派工、結束時不等待；`finally` 還會直接釋放所有 url/proxy locks，包括仍被 in-flight chunk 持有的）
  - 問題：`ChunkDownloadFailed` 或取消後，仍在串流中的 chunk threads 不會被等待就返回。CLI 情境下 process 退出會硬殺 daemon threads（part 檔可能寫一半）；GUI 情境更糟 —— 使用者對同一檔案立刻重試時，舊執行緒可能仍在對同一批 `tmp/<filename>.partNN` 路徑寫入，與新一輪下載互踩導致損毀。
  - 建議做法：`_run_scheduling_loop` 追蹤派出的 `Thread` handles，退出前逐一 `join(timeout=...)`（chunk 執行緒本身已會因 `stop_event` 提早結束，join 是收尾保證）；lock 釋放移到 join 之後。
  - 測試：取消後斷言所有 chunk threads 已結束、無執行緒殘留寫檔。

## R2-P1 — 實際使用必踩的輸入/平台相容性（對 Windows 目標尤其重要）

- [x] **R2-4 伺服器回傳的檔名未 sanitize → Windows 非法字元使所有 chunk 落地失敗**（2026-07-17 完成；測試：`tests/test_downloader_filename_and_paths.py::TestSanitizeFilenameComponent`、`TestResolveFilenameSanitizesServerName`、`TestResolveFilenameUserSuppliedName`。實作：新增模組級 `_sanitize_filename_component`（替換非法字元/控制字元、處理保留字裝置名、去除結尾空白與句點、空結果 fallback 成 `"download"`），在 `_resolve_filename` 對 `original_name` 與使用者提供檔名的最終路徑成分（保留原本目錄結構）統一套用。）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_resolve_filename` / part 檔命名 / `_merge_parts`），檔名來源 `k2s_client.get_name`
  - 問題：Keep2Share 回傳的原始檔名可能含 Windows 不允許的字元（`\ / : * ? " < > |`）或保留字（`CON`、`NUL`…）。目前直接拿來組 part 檔與最終檔路徑，在 Windows 上 `write_bytes` 會直接 `OSError` → 被 `_mark_chunk_failed` 當成一般錯誤重試 8 次後丟 `ChunkDownloadFailed`，錯誤訊息還誤導成「IP/proxy 被封鎖」。本專案目標平台就是 Windows（見 AGENTS.md §7），這是高機率實際踩到的問題。
  - 建議做法：新增檔名 sanitize（替換非法字元、處理保留字與結尾空白/句點），在 `_resolve_filename` 統一套用；失敗時錯誤訊息應能區分「本地寫檔失敗」與「網路失敗」。
  - 測試：對含各非法字元/保留字的檔名驗證 sanitize 結果；驗證磁碟寫入失敗不會被誤報成封鎖。

- [x] **R2-5 `filename` 含目錄成分時 part 檔路徑父目錄不存在 → 全數失敗**（2026-07-17 完成；測試：`tests/test_downloader_filename_and_paths.py::TestPartPathStaysFlatUnderTmpDir`、`TestMergePartsCreatesTargetParentDirectory`。實作：新增 `Downloader._part_path()` 統一以 `Path(filename).name`（不含目錄成分）組出 part 檔路徑，`_download_chunk`／排程重用分支／`_merge_parts` 三處呼叫點皆改用它；`_merge_parts` 寫出最終檔前先 `target_path.parent.mkdir(parents=True, exist_ok=True)`。附帶修正 `tests/test_downloader_status_code.py` 的既有 fixture：該測試直接呼叫 `_download_once` 而略過 `download()` 原本會做的 `tmp_dir.mkdir(...)`，先前靠絕對路徑覆蓋 `tmp_dir` 前綴的巧合才沒有暴露這個缺口，修正後需要顯式建立 `tmp_dir`。）
  - 位置：`src/k2s_downloader/core/downloader.py`（part 檔路徑 `self.tmp_dir / f"{ctx.filename}.partNN"`）
  - 問題：CLI `--filename out/video.mp4` 這類含路徑的值會使 part 檔路徑變成 `tmp/out/video.mp4.partNN`，`tmp_dir.mkdir` 只建立 `tmp/`，寫入時 `FileNotFoundError` → 同 R2-4 的誤導性重試循環。
  - 建議做法：part 檔一律只用 `Path(filename).name` 命名；最終輸出前確認/建立目標父目錄。
  - 測試：`filename` 含相對路徑時 part 檔落在 `tmp/` 平面、最終檔寫到指定路徑。

- [ ] **R2-6 `_fetch_total_size` 不檢查 HTTP status → 錯誤頁的 Content-Length 被當成檔案大小**
  - 位置：`src/k2s_downloader/core/downloader.py`（`_fetch_total_size`）
  - 問題：HEAD 回 403/429/5xx 時仍讀 `Content-Length`（錯誤頁大小），切出完全錯誤的 ranges，之後每個 chunk 都 size mismatch，浪費整輪重試才失敗，且訊息不指向真正原因（URL 過期/被封鎖）。
  - 建議做法：非 2xx 直接丟 `RuntimeError`（訊息含 status code 與「download URL 可能已過期或被封鎖」提示）。
  - 測試：mock HEAD 回 403 驗證立即失敗且訊息正確。

## R2-P2 — 資源使用與死碼

- [ ] **R2-7 每個 chunk 整段緩衝在記憶體 → 峰值可達數百 MiB**
  - 位置：`src/k2s_downloader/core/downloader.py`（`_download_chunk` 的 `io.BytesIO`；`_merge_parts` 的一次性 `chunk.read()`）
  - 問題：預設 20 threads × ≥20MiB split ≈ 400MiB 峰值；媒體檢查失敗重試會把 split 加倍再翻倍。對打包成 exe 給一般 Windows 使用者的情境不友善。
  - 建議做法：chunk 改為邊下邊寫暫存檔（`.partNN.tmp` 完成後 rename 成 `.partNN`，rename 的原子性同時消除「寫一半的 part 被重用分支誤判完整」的風險）；`_merge_parts` 改 `shutil.copyfileobj` 串流合併。
  - 測試：驗證 rename 前的 `.tmp` 不會被排程重用分支撿走；合併結果 byte-identical。

- [ ] **R2-8 死碼 `_load_cached_urls`**
  - 位置：`src/k2s_downloader/core/downloader.py`（`_load_cached_urls`，全 repo 無呼叫端也無測試）
  - 問題：`download()` 開頭固定刪除 URL cache 檔再重建，`_load_cached_urls` 從未被呼叫 —— 「URL 快取重用」這個功能只做了寫入端。download URL 本身有時效，跨 session 重用價值本來就低。
  - 建議做法：直接刪除該 method（連同評估 `url_cache_path`/`urls.json` 是否還有存在必要 —— 若只剩除錯用途，在 docstring 註明）。

## R2-P3 — Windows exe 打包（PyInstaller）

- [ ] **R2-9 exe 打包整備**：基礎已可行 —— `gui/app.py` 已處理 `sys._MEIPASS` 資源路徑、`pyproject.toml` 已有 `[build]` extra（`pyinstaller>=6.16.0`）、`core/` 無 GUI 依賴。但以下缺口需補齊（依阻斷程度排序）：
  1. **（阻斷）可寫路徑問題**：`tmp/`、`urls.json`、`proxies.txt` 與最終下載檔全部寫在 CWD。雙擊 exe 時 CWD 是 exe 所在目錄，裝在 `Program Files` 下會直接 `PermissionError`。`Downloader` 已有 `tmp_dir`/`url_cache_path`/`proxy_cache_path` 參數但 `gui/worker.py` 全用預設值 —— 需改為使用者資料目錄（`QStandardPaths.AppDataLocation`），並在 GUI 加「下載儲存位置」選擇（目前完全沒有）。
  2. **（阻斷，同 R2-4）** 檔名 sanitize 必須先修，否則 Windows 上高機率第一次下載就失敗。
  3. **spec 檔／build 腳本**：尚無。需要 `--windowed --icon src/assets/icon/icon.ico`、`--add-data resources/style.qss;resources`、`--add-data src/assets/icon/icon.ico;assets/icon`（對應 `_resource_path` 期望的 `_MEIPASS` 內相對路徑）。建議 onedir 而非 onefile（onefile 較易觸發 Defender/SmartScreen 誤報，且啟動慢）；未簽章 exe 的 SmartScreen 警告需在 Readme 說明。
  4. **需要「去除」的元素**：CLI 入口（`k2s-downloader`）不要包進 windowed exe —— `default_captcha_callback` 用 `Image.show()` + `input()`，windowed 模式無 stdin 會掛死；GUI 有自己的 captcha callback 不受影響。若也要發佈 CLI exe，需另出 console build。tqdm/print 在 windowed 下無害（GUI 路徑 `show_console_progress=False`）。
  5. **不打包的外部依賴**：`ffmpeg` 維持現狀（`which("ffmpeg")` 找不到就跳過媒體檢查），在發佈說明註明即可。體積優化（PySide6 excludes）為選配。
  6. 建議加一個 CI job（或至少文件化的本機指令）驗證打包產物能啟動。

## R2-P4 — proxy 來源與生命週期管理（回應「proxyscrape 過時 proxy」問題）

- [ ] **R2-10 proxy pool 品質改善**。現況：單一來源 `api.proxyscrape.com`（**v1 舊版 API**，官方已遷移至 v2/v3，v1 隨時可能停止服務 —— 屆時 `fetch_remote` 拿到空清單、退化成純直連而無明顯錯誤）；驗證只打 `api.myip.com`；`proxies.txt` 快取無 TTL；`working_proxy_indexes` 只進不出。免費公開 proxy 本質就是高汰換率＋MITM 風險（見 P2-3），**換一家來源只能緩解、不能根治**，重點應放在驗證與生命週期：
  1. **升級／多來源**：改用 proxyscrape v2 endpoint，並把來源抽象成 provider 清單，聚合 GitHub 上定時更新的免費清單（如 `TheSpeedX/PROXY-List`、`monosans/proxy-list`、`proxifly/free-proxy-list` 的 raw URL）後去重 —— 多來源聯集能顯著提高「當下活著」的比例。
  2. **驗證目標對齊**：`api.myip.com` 可達 ≠ 該 proxy 沒被 Keep2Share 封鎖。驗證改為（或加驗）對 `k2s.cc` 的輕量請求，直接篩掉對目標站無效的 proxy。（共享的公開 proxy 很可能早已被 k2s 封鎖或限制，這點讓「對目標站實測」比「泛用可達性檢查」重要得多。）
  3. **快取 TTL**：`proxies.txt` 加時間戳，超過（例如 6~24 小時）自動視為過期觸發 refresh；啟動時可預設走已有的 `recheck_cached` 路徑。
  4. **Runtime 降級**：proxy 進入 `working_proxy_indexes` 後即使開始連續失敗仍會被優先選中 —— 對每個 index 記錄連續失敗次數，超閾值即自 working 清單移除。
  5. **使用者自備清單**：CLI flag／GUI 匯入自有 proxy 清單，讓在意 MITM 的使用者完全繞開公開清單。
  6. 非 proxy 替代方案評估過不採用：Tor（免費多出口但慢、出口常被檔案站封鎖）、免費 VPN（不可程式化輪替）。是否真的需要 proxy，由 R2-11 的量測數據決定。

## R2-P5 — 根本目的（突破 50KB/s）達成度檢視

- [ ] **R2-11 用量測數據驗證加速機制、據此決定 proxy 架構去留**
  - **現況評估**：對照根本目的（瀏覽器免費下載被限 50KB/s），加速機制在程式碼層面已完整 —— 單次 captcha → 產生 N 個 download token → byte-range 並行下載 → 直連優先＋proxy fallback＋重試/backoff＋取消＋CLI/GUI 雙前端，且第一輪 P0~P5 已把掛死/損毀類缺陷清完。**「紙面上」目的已達成，但缺實測數據佐證**（本輪環境無法連 Keep2Share 驗證）。
  - **已知事實（2026-07-17 使用者第一手經驗；除最後一點外皆為本 app 實際使用觀察）**：
    - website（free plan）下載被限 50KB/s，且 browser 下載非常容易中斷（原因不明）—— 這兩點是本 app 存在的原始動機。
    - **本 app 通過 captcha 開始下載後，實測速度約 1~3MB/s** —— 相對 50KB/s 是 20~60 倍加速，**主體下載階段的根本目的已實際達成**。
    - **下載 >9GB 的大檔時，接近 99% 完成度速度會掉到約 10~50KB/s**（詳見 R2-12 的成因分析與對策）。
    - 「同一 IP 累計約 9GB 後被封鎖一到兩天、換動態 IP 即恢復」為 website 使用時期的**體感推測，未經證實**——依使用者判斷，**不做專用的 9GB 偵測/計量功能**，只保留通用的「疑似被封鎖」提示。
  - **由 1~3MB/s 這個數字可得的推論**：聚合速度 ≈ 連線數 × 50~150KB/s，強烈暗示免費限速是 **per-connection（per-token）** 而非 per-IP 總量 —— 這正是本 app 多 token 並行設計能生效的原因。待 telemetry 佐證後，proxy pool 的定位可再收斂（另注意：`_acquire_proxy_lock` 的直連 slot 只有一個 lock，同時間僅一條 chunk 走直連，其餘都經 proxy —— 1~3MB/s 的流量組成到底直連佔多少、proxy 佔多少，是 telemetry 要回答的第二個問題）。
  - 建議做法：
    1. 增加 per-chunk／per-proxy 吞吐統計（經由既有 `status_callback`／GUI dev panel 呈現，`core/` 不加 print/logging），驗證上述 per-connection 推論，並量測直連 vs proxy 的流量占比。
    2. 依數據調整預設：若直連並行即可滿速，proxy 改為 opt-in，砍掉最大風險面。
    3. 偵測到疑似封鎖（`ChunkDownloadFailed`／captcha 連續被拒）時，狀態訊息提示「若為動態 IP，可嘗試重啟數據機換 IP 後重試」（零成本、比依賴不可信 proxy 安全；不涉及任何額度計量）。
    4. （選配）提供「匯出 download URLs 為 aria2c input file」功能，複用成熟分段下載引擎作 A/B 對照與備援路徑。
  - **附帶事實記錄**：排程迴圈的 part 檔重用分支（size 相符即採用）已天然提供同機續傳雛形 —— 中斷後重跑會跳過已完成的 part，只差「URL 過期後重新產生再接續」的串接。正式的續傳功能已因使用者需求立項為 R2-13。

- [ ] **R2-12 大檔接近 99% 時速度崩落（10~50KB/s）的成因與對策**
  - **現象**（使用者實測）：>9GB 檔案下載至約 99% 時，速度從 1~3MB/s 掉到 10~50KB/s。
  - **最可能成因：平行度尾端崩落（long-tail collapse），不需要任何「額度碰頂」假設**。固定 20MiB split 下，>9GB 檔 ≈ 460+ 個 chunk、20 條連線；當「剩餘 chunk 數 < 連線數」時，活躍連線隨完成逐一歸零，最後只剩 1~2 條 —— 聚合速度自然掉回**單連線速度**，而觀察到的 10~50KB/s 恰好就是免費 per-connection 限速的量級，與推論吻合。最後一個 20MiB chunk 以 50KB/s 下載需時約 7 分鐘，體感上就是「卡在 99% 很久」。
  - **次要成因（可並存）：倒楣 chunk 的重試損耗**。尾端殘留的常是反覆失敗的 chunk：每次失敗整段 buffer 丟棄、進度回退（`_report_progress(-chunk_bytes)`）、backoff 最長 30s、下次還可能抽到另一個爛 proxy —— 有效吞吐趨近於零。R2-10 第 4 點（proxy 失敗降級）與 R2-7（改寫入暫存檔）都會直接改善這一項。
  - 對策選項（由簡到難）：
    1. **尾端優先直連**：剩餘 chunk 數低於門檻時，改為優先等待直連 slot 而非退而求其次抽 proxy（直連品質通常最穩，尾端最忌諱抽到爛 proxy 重來）。改動小。
    2. **尾端 chunk 冗餘派工（speculative duplication）**：剩餘 chunk < 空閒連線數時，把同一 range 同時派給多條空閒連線，先完成者勝、其餘取消。頻寬浪費有限（只發生在尾端），實作比動態切分簡單，aria2 類工具的常見手法。
    3. **動態範圍再切分（work stealing）**：尾端把仍在下載中的大 range 對半分給空閒連線。效果最好但需要支援「部分 range 的銜接合併」，改動最大。
    4. （輔助）縮小預設 split size 或改用「檔案越大、尾段 split 越小」的遞減切分 —— 直接縮短尾端長度，零架構改動，可先行。
  - **驗證方式**：R2-11 的 telemetry 先行 —— 尾端同時記錄「活躍連線數」與「聚合速度」，若兩者同步下降即證實主因是平行度崩落（而非封鎖/額度），再依數據挑選上面哪個對策。
  - 測試：以 mock 驗證尾端派工策略（冗餘派工的先完成者勝出、輸家取消不寫檔）；切分策略的 ranges 正確性沿用 `TestBuildRangesContiguity` 模式。

- [ ] **R2-13 可見且可靠的斷點續傳（使用者實際需求，2026-07-17 立項）**
  - **使用者痛點**：下載過程在磁碟上看不到任何暫存檔／進度落地，中斷後不確定上次下載到哪、下次能不能接續、甚至懷疑根本沒下載到任何東西。
  - **成因分析**（對照現有程式碼）：
    1. part 檔只在 chunk **完整下載後**才一次寫入（`_download_chunk` 的 `io.BytesIO` 緩衝，見 R2-7）—— 下載中資料全在 RAM，磁碟上看不到成長中的檔案，中斷即全部丟失。
    2. `tmp/` 位置取決於 process 的 CWD（見 R2-9.1），使用者不易找到，也沒有任何 UI 顯示暫存位置。
    3. 續傳條件隱性且脆弱：排程迴圈的 part 重用要求「同 filename＋同 split 佈局」（part 檔名 zfill 依 split_count，size 逐一比對）才會生效，使用者不知道這些條件，重跑時也沒有任何訊息告知「找到上次進度，將接續」。
    4. 沒有 manifest：中斷後磁碟上只有一堆 `.partNN`，沒有任何記錄說明它們屬於哪個 file_id、total size、split 佈局 —— 只能靠檔名＋大小巧合匹配，無從驗證。
  - **建議做法**：
    1. **下載 manifest**（如 `tmp/<filename>.manifest.json`）：記錄 file_id、原始 URL、total_size、split_size、ranges 佈局、各段完成狀態與時間戳。開始下載時偵測同名 manifest：total_size 與 split 佈局相符 → 進入續傳模式（重新走 captcha／URL 產生流程後，跳過已完成段）；不符 → 明確告知後全新下載。
    2. **串流寫入落地**（R2-7 為前置或同步做）：chunk 改為邊下邊寫 `.partNN.tmp`、完成後 rename 成 `.partNN` —— 「下載中」在磁碟上即時可見，中斷最多損失單一 chunk 的未完成部分，rename 原子性同時保證 `.partNN` 一定是完整的。
    3. **可見性（UI/狀態訊息）**：下載開始時回報「找到上次進度 X/Y 段（共 Z MiB），接續下載」或「無上次進度，全新下載」；GUI 顯示暫存目錄路徑並提供「開啟資料夾」；進度列區分「本次下載」與「先前已完成」的量。
    4. **清理策略**：成功合併後刪除 manifest 與殘留 part；使用者取消或失敗中止時**保留**（供續傳）並在訊息中告知暫存位置。
  - **範疇註記**：`docs/ai/requirements.md` 目前把跨 process 續傳列為 out-of-scope —— 本項是使用者明確提出的需求變更，實作時需同步以最小 diff 更新 spec（新增對應 REQ/AC）與 `docs/human/requirements.md`。
  - **依賴**：R2-7（串流寫入）為核心前置；R2-9.1（使用者資料目錄）決定 tmp 的最終位置，建議一起規劃。
  - 測試：manifest 寫入/讀取/相符判斷；split 佈局不符時拒絕誤續傳（fallback 全新下載並告知）；中斷→重跑跳過已完成段且 byte-identical；`.tmp` 未完成檔不被重用分支誤判為完整。

---

## 建議處理順序

1. ~~先做 **P0-1、P0-2** 並同步補 **P3-1、P3-2**（純函式、好測、風險高）。~~ ✅ 已完成
2. ~~再處理併發相關 **P0-3、P0-4** 與掛死相關 **P0-5、P0-6**。~~ ✅ 已完成 — **P0 全部 6 項皆已修復**
3. ~~接著 **P1**（依賴／CI／文件入口）讓專案可持續驗證。~~ ✅ 已完成（P1-1 ~ P1-4 全數完成）
4. ~~之後 **P2**（錯誤處理與健壯性）。~~ ✅ 已完成 — **P2 全部 4 項（P2-1 ~ P2-4）皆已修復，含 P3-4**
5. ~~接著 **P4 → P5** 逐步推進。~~ ✅ 已完成 — **P4 全部 3 項（P4-1 ~ P4-3）、P5 全部 4 項（P5-1 ~ P5-4）皆已完成**

> 註：P0（P0-1 ~ P0-6）、P1（P1-1 ~ P1-4）已於 2026-07-16 完成並合併進 `main`（PR #3，merge commit `8581b77`）。
> P2（P2-1 ~ P2-4，含補齊的 P3-4）已於 2026-07-17 完成並合併（PR #6，merge commit `71eb5f5`）。
> P4（P4-1 ~ P4-3）、P5-1 ~ P5-3 已於 2026-07-17 完成，分支 `feature/p4-p5-quality-and-docs`。
> P5-4（本次文件依受眾重新分類）於 2026-07-17 完成，分支 `docs/reorganize-ai-human-audience`。
> 所有測試（本機 `.venv`，`pytest -q`，`pyproject.toml` 已設 `pythonpath=["src"]` 免手動設環境變數）
> 共 74 個全數通過（連續跑 5 次確認無 flaky），`ruff check .` 全過。
>
> **第一輪 P0 ~ P5 六個優先級皆已全數完成。** 2026-07-17 已依「重新從頭檢視現況」原則完成第二輪靜態
> code review，新一輪 backlog 見上方「第二輪（R2）」段落（R2-1 ~ R2-11，全部未認領）。
> 建議接手順序：R2-P0（並發 race，改動小、風險高）→ R2-4/R2-5（Windows 相容性，是 exe 打包的前置）
> → **R2-7＋R2-13（串流寫入＋斷點續傳，使用者明確需求，兩項綁定實作）** → R2-9（打包，含 R2-9.1
> 使用者資料目錄，與 R2-13 的 tmp 位置一起規劃）→ 其餘依需求。R2-11 的 telemetry 是 R2-10（proxy
> 投資深度）與 R2-12（99% 尾端崩落對策選擇）共同的前置驗證，三項建議一起規劃；R2-12 的對策 4
> （縮小尾端 split）零架構改動可先行。
