# K2S Downloader — 架構文件

> 人類可讀的繁體中文版本；AI 工具請改讀 canonical 版本 [docs/ai/architecture.md](../ai/architecture.md)。本 repo 閱讀順序：[AGENTS.md](../../AGENTS.md) → docs/ai/requirements.md → docs/ai/architecture.md → 其他文件。安裝與使用說明見人類可讀的 [Readme.md](../../Readme.md)；貢獻流程見 [CONTRIBUTING.md](../../CONTRIBUTING.md)。

## 1. 模組地圖

```
src/k2s_downloader/
├── __main__.py          # `python -m k2s_downloader` -> cli.main()
├── cli.py                # argparse CLI 前端
├── core/                 # 不依賴任何 GUI toolkit，可獨立 import／測試
│   ├── downloader.py      # Downloader：整體協調、切分區段、排程與下載區塊、合併
│   ├── k2s_client.py      # Keep2Share API client：captcha、URL 產生、檔名查詢
│   └── proxy.py           # 公開 proxy 清單的抓取／驗證／快取
└── gui/                  # PySide6 前端，只透過 core/ 的 callback 介面互動
    ├── app.py              # QApplication 啟動、載入 stylesheet／icon
    ├── main_window.py      # MainWindow：所有 widget、signal 連接、UI thread 狀態
    └── worker.py           # DownloadWorker / ProxyLoaderWorker：包住 core.Downloader 的 QThread

k2s_gui_entry.py          # 給 PyInstaller 打包用的輕量入口腳本 -> gui.app.main()
tests/                    # 對應 core/ 各模組；所有 requests 呼叫都 mock；不含 GUI 測試（見 NFR-4）
```

`core/` 是唯一包含商業邏輯的套件。`cli.py` 與 `gui/` 都只是薄前端：建立 `Downloader`、接上 callback／signal，其餘不介入。

## 2. 控制流程

```
CLI/GUI
  └─ Downloader.download(url, ...)
       ├─ stop_event.clear()                              # AC-7.2
       ├─ refresh_proxies()                                 # 若尚未取得 proxy 清單
       ├─ extract_file_id(url)                              # REQ-1
       ├─ k2s_client.get_name(file_id)                       # REQ-2（檔名）
       ├─ k2s_client.generate_download_urls(...)              # REQ-5（captcha）+ 每個 thread 一個 URL
       │     ├─ fetch_captcha() -> captcha_callback(...)
       │     └─ 針對每個 proxy：解 captcha -> free_download_key -> 批次取得 `count` 個 URL
       └─ 迴圈（最多兩次，對應 AC-8.2 的媒體檢查重試）：
             └─ _download_once(urls, filename, threads, split_size)
                  ├─ _fetch_total_size(url, headers)          # REQ-2（大小，HEAD 請求）
                  ├─ _build_ranges(total_size, split_count)     # REQ-3（連續 byte range）
                  └─ _run_scheduling_loop(ranges, ...)          # REQ-3/4（派工＋重試／backoff）
                        └─ 每個區段各自在獨立 thread 執行：
                              _download_chunk(...)
                                ├─ _acquire_proxy_lock()         # REQ-6（直連優先）
                                ├─ requests.get(Range: bytes=...)
                                └─ 成功：寫入 .partNNN，標記該區段完成
                                   失敗：_mark_chunk_failed() -> backoff 或永久失敗
                  └─ _merge_parts(ranges, filename, split_count)  # AC-3.6（串接＋清理）
```

`download()` 是前端唯一會呼叫的公開入口。`_download_once` 底下的內容都是 `Downloader` 的 private 實作細節，目的是讓 `_download_once` 本身保持精簡 — 見第 5 節。

## 3. Threading 與並發模型

`Downloader` 的排程迴圈跑在呼叫端自己的 thread（`download()` 會阻塞直到檔案完成、被取消、或失敗），並為每個進行中的區段各自產生一個短生命週期的 `threading.Thread`，最多同時 `threads` 個。這裡沒有 thread pool；thread 建立後各自完成／結束（`daemon=True`），透過搶佔固定數量的 `url_locks` 其中一個來控管。

| 共用狀態 | 保護機制 | 說明 |
|---|---|---|
| `working_proxy_indexes`（已知可用的 proxy 索引清單） | `_working_proxy_lock` | 多個下載 thread 會讀取（複製）與新增；曾經完全無鎖保護、存在 race condition，改用專屬 lock 修復。 |
| `proxy_locks[i]`（每個 proxy 一個 lock） | 自身 | 透過非阻塞的 `acquire(blocking=False)` 迴圈取得（`_acquire_proxy_lock`），絕不使用 blocking acquire — 原因見第 4 節。 |
| `_active_proxy_indexes` | `_proxy_state_lock` | 供 UI／狀態快照讀取（`_notify_proxy_state`）；區塊下載開始／結束時寫入。 |
| `_bytes_downloaded`、`_done_count` | `_progress_lock` | 任何區塊下載 thread 透過 `_report_progress` / `_download_chunk` 做 read-modify-write。 |
| `url_locks[i]`（每個 thread 槽位一個） | 自身 | 控管同時在跑的區塊下載 thread 數量；排程迴圈在產生 thread 前搶佔，該 thread 完成時釋放（或在 `_run_scheduling_loop`/`_download_once` 提早結束時由 `finally` 清理）。 |
| `stop_event`（`threading.Event`） | 不需要 — 本身就是 thread-safe | 取消狀態的唯一真相來源；見第 4 節。 |

### 為什麼取消狀態只用一個 Event，而不是額外鏡射一個 boolean

`_download_chunk` 和排程迴圈都需要知道「這次下載是否已被取消」。舊實作在 `_download_chunk` 裡用一個透過 closure 在所有區塊下載 thread 間共用的 `nonlocal stop` boolean 來鏡射這個狀態。但每一條會設定它的路徑，都只在 `self.stop_event` 已經被設定（或同一段程式碼裡正在被設定）時才會觸發 — 也就是說這個 boolean 完全是多餘的，而且比直接檢查 `self.stop_event.is_set()` 更冒風險（額外的跨 thread 共享可變狀態）。現在的實作就是直接這樣做：`_download_chunk` 完全不碰任何 stop 旗標；`_run_scheduling_loop` 保留了一個「本地、單一 thread 使用」的 `stop_scheduling` boolean，但只是為了同時涵蓋另一個跟 `stop_event` 無關的跳出條件（某個區段永久耗盡重試次數）。

### 為什麼 `_acquire_proxy_lock` 絕不阻塞

舊實作是先掃描哪個 proxy lock 回報 `locked() == False`，再對它呼叫「阻塞」的 `.acquire()` — 這是一個 check-then-act 的 race：兩個 thread 可能同時看到同一個 lock 是空的，敗者就會卡在 `.acquire()` 裡無限期等待，同時仍持有它的 `url_locks` 槽位，等於讓那個 thread 槽位死鎖。`_acquire_proxy_lock` 改成用迴圈呼叫 `acquire(blocking=False)`（絕不阻塞），每次嘗試間短暫 sleep，並在 `stop_event` 被設定時直接放棄（`return None`）— 確保就算所有 proxy 都暫時忙碌，也能持續有進展，取消請求也能立即被回應。

## 4. Proxy 處理設計

`proxy.py::get_working_proxies()` 是唯一的 proxy 候選來源。其回傳值第 0 個元素恆為 `None`（代表「不用 proxy，直連」）— 這個不變量是關鍵依賴：`Downloader._acquire_proxy_lock()` 會檢查 `self.proxies[0] is None`，並永遠優先嘗試搶佔這個槽位，才會 fallback 到已知可用或隨機選擇的 proxy。候選清單來自 `proxyscrape.com`（公開、未經驗證的清單），僅做輕量驗證（對 `api.myip.com` 做一次 HTTPS 可達性測試）；驗證過的結果會快取到磁碟（`cache_path`，可設定，預設為 CWD 下的 `proxies.txt`），除非要求 `refresh=True` 或 `recheck_cached=True`，否則之後的執行可以跳過重新驗證。

安全性立場（另見 [Readme.md](../../Readme.md) 的「Security Note: Public Proxies」段落）：proxy 這段連線本身即使目標網址是 HTTPS，也是未經驗證的明文 HTTP（`requests` 是透過 HTTP `CONNECT` 把 HTTPS 隧道到這個 proxy），因此惡意的 proxy 操作者有能力觀察或竄改經過的流量。這是為了規避單一 IP 速率限制而刻意做的取捨並已明確記載，不是疏忽 — 直連永遠優先（見第 3 節），proxy pool 純粹是 fallback。

**吞吐量 telemetry**（見 [`todolist.md`](todolist.md) R2-11）：`_report_progress` 可選地從 `_download_chunk` 接收 `is_direct: bool`（`proxy_idx == 0`），把即時連線下載到的位元組記到 `_direct_bytes_downloaded` 或 `_proxy_bytes_downloaded`；未經即時連線取得的位元組（排程迴圈的 part 重用分支，接續已在磁碟上的區段）則傳入 `is_direct=None`，不計入這個統計。`_run_scheduling_loop` 每次 poll 都會呼叫 `_maybe_report_throughput()`，最多每 `TELEMETRY_REPORT_INTERVAL`（5 秒）透過 `status_callback` 回報一次訊息（聚合速度、直連／proxy 百分比、活躍連線數）。這是為了讓實際使用能驗證或推翻「本專案的速度上限是 per-connection 而非 per-IP」這個工作假設而存在——這份 instrumentation 本身不做任何決定，R2-10／R2-12 的 proxy 投資與尾端崩落對策仍需要真實觀測數據。

## 5. 錯誤分類

| 例外 | 何時丟出 | 對呼叫端的意義 |
|---|---|---|
| `DownloadCancelled` | `download()` 執行期間任何時候 `stop_event` 被設定（透過 `Downloader.cancel()`） | 「使用者／呼叫端自己停止的」，不算錯誤。 |
| `ChunkDownloadFailed` | 單一 byte-range 區段耗盡 `MAX_CHUNK_RETRIES`（8）次嘗試 | 「我們放棄了」。與 `DownloadCancelled` 區分開，讓呼叫端可以針對「你自己停的」跟「跑不完」分別呈現不同的 UI／exit code。 |
| `K2SFileNotFound` | Keep2Share API 回報檔案不存在 | 可被捕捉、非致命 — 取代了舊版會從背景 thread 直接 `sys.exit()` 殺掉整個 process（含 GUI）的行為。 |
| `OperationCancelled`（在 `k2s_client` 內） | 在 captcha／URL 產生階段（尚未開始任何區塊下載前）`stop_event` 被設定 | 會被 `Downloader.download()` 捕捉並重新丟成 `DownloadCancelled`，讓呼叫端無論在哪個階段被取消，都只需處理一種取消例外類型。 |
| `RuntimeError`（各種訊息） | 查詢大小／檔名時網路不可達；captcha 答錯達 `MAX_CAPTCHA_ATTEMPTS` 次；所有 proxy 都試過仍拿不到任何可用 URL；大小無法判斷 | 每則訊息都會指出可能原因（IP 被封鎖、速率限制等），而不是直接丟出原始的 `requests` 例外。 |
| `ValueError` | 無效的 URL（`extract_file_id`）；`split_size` 低於 5 MiB 下限；無法解析的 `--split-size` 字串 | 呼叫端／輸入驗證錯誤，在任何網路活動發生前就丟出。 |

重試／backoff 參數（`downloader.py` / `k2s_client.py` 中的模組層級常數，刻意集中管理而非寫死內聯 — 見 [`docs/ai/todolist.md`](../ai/todolist.md) 的 P2-2）：

| 常數 | 值 | 控制對象 |
|---|---|---|
| `MAX_CHUNK_RETRIES` | 8 | 單一 byte-range 區段在丟出 `ChunkDownloadFailed` 前的嘗試次數 |
| `CHUNK_RETRY_BACKOFF_BASE` / `_CAP` | 1.0 秒 / 30.0 秒 | 區段重試之間的指數 backoff |
| `MAX_CAPTCHA_ATTEMPTS` | 3 | 放棄前允許 captcha 答錯的次數 |
| `MAX_URL_BATCH_ROUNDS` | 3 | 同一個 proxy 連續幾輪拿不到任何新 URL 就換下一個 |

### Timeout 一覽

本專案每一個對外 HTTP 呼叫都帶有明確 timeout（NFR-1）；沒有任何呼叫依賴 `requests` 的預設值（也就是完全不設 timeout、可能永久阻塞）。

| 常數 | 模組 | 值 | 用途 |
|---|---|---|---|
| `HEAD_REQUEST_TIMEOUT` | `downloader.py` | 15 秒 | 查詢檔案總大小 |
| `CHUNK_REQUEST_TIMEOUT` | `downloader.py` | 20 秒 | 每個區段 `GET` 的 connect/read timeout |
| `CHUNK_STALL_TIMEOUT` | `downloader.py` | 20 秒 | 獨立的停滯監控：即使 socket 本身沒 timeout，只要這段時間內沒有新資料進來就放棄這次嘗試 |
| `DEFAULT_TIMEOUT` | `k2s_client.py` | 15 秒 | Keep2Share API 一般呼叫（取得 captcha、查詢檔名、批次產生 URL） |
| `CAPTCHA_SOLVE_TIMEOUT` | `k2s_client.py` | 5 秒 | 專門給每個 proxy 的 captcha 解答探測 — 刻意設短，避免單一失效 proxy 卡住整個 captcha 迴圈 |
| `HTTPS_TIMEOUT` | `proxy.py` | 5 秒 | Proxy 驗證階段對每個候選的可達性測試 |
| `PROXYSCRAPE_FETCH_TIMEOUT` | `proxy.py` | 30 秒 | 抓取原始候選清單（單一較大請求，非逐一探測） |

## 6. GUI 整合

`gui/worker.py` 用兩個 `QThread` 子類別包住 `core.Downloader`，確保 Qt 事件迴圈絕不會被網路 I/O 卡住：

- **`DownloadWorker`**：在一次下載的生命週期內持有一個 `Downloader` instance，把 `Downloader` 的純 callback 介面（`status_callback`、`progress_callback`、`proxy_state_callback`、`captcha_callback`）橋接成 Qt signal。進度與 proxy 狀態更新在跨越 thread 邊界前都會先節流（`_progress_emit_interval`、`_proxy_emit_interval`）— 這正是 AC-10.2 所指的機制；沒有這層節流，高 thread 數會讓 UI thread 被大量 signal 灌爆而卡死。Captcha callback 會讓 worker thread 卡在一個 `threading.Event` 上，直到 `MainWindow.submit_captcha()` 從 UI thread 把它設定為止。
- **`ProxyLoaderWorker`**：另一個較單純的 `QThread`，專門處理「重新整理 proxy 清單」的動作，不需要有下載正在進行、也不會卡住 UI。

`main_window.py` 的 `MainWindow` 擁有所有 widget，並保存只屬於 UI thread 的狀態（平滑化後的下載速率／ETA、log buffer）。它從不直接碰 `Downloader`，只透過每次下載自己建立、並在 `_reset_state()` 中收尾的 `DownloadWorker` instance 互動。

## 7. 測試結構

`tests/` 大致依「關注點」而非嚴格依檔案對應 `core/`：

- `test_downloader_units_and_ranges.py` — 純函式／單元測試：`parse_size`、`_build_ranges`、`_acquire_proxy_lock` 的並發安全性。
- `test_downloader_error_handling.py` — 區塊層級的例外處理（request 錯誤 vs. 非預期錯誤，兩者都會被記錄而非吞掉）。
- `test_downloader_status_code.py` / `test_downloader_timeouts.py` / `test_downloader_retry_limit.py` — HTTP 狀態碼處理、timeout 傳遞、重試／backoff 耗盡行為。
- `test_k2s_client_timeouts.py` / `test_k2s_client_blocked.py` — captcha／URL 產生階段的 timeout 與有界重試行為。
- `test_proxy_preference_and_cache.py` — 直連優先、可設定的快取路徑、`get_working_proxies` 的 cached/refresh/recheck_cached 三條路徑。
- `test_human_readable_bytes.py` — 顯示用單位換算格式化。

每個測試都在呼叫點 mock `requests`（`patch("k2s_downloader.core.downloader.requests.get", ...)` 等）— 沒有任何測試會真的打網路。`gui/` 依設計沒有測試覆蓋（`# pragma: no cover - GUI wiring`）；要有意義地測試它需要一個實際跑起來的 Qt 應用程式，且其邏輯刻意保持精簡（只做狀態橋接、沒有商業邏輯），所以這個缺口風險低。

## 8. CI

`.github/workflows/ci.yml` 在每次 push 到 `main` 與每個 pull request 上執行 `ruff check .` 與 `pytest -q`，涵蓋 Python 3.9（`requires-python` 下限）與 3.13。`.github/workflows/ai-review.yml` 則是獨立的機制，會在 pull request 上留下 LLM 產生的 review 留言（與 test/lint 的把關機制無關）。
