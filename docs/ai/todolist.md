# K2S Downloader — 優化待辦清單 (todolist)

> 本檔提供給後續 session 接手用。每項含：**問題**、**位置**、**建議做法**、**狀態**。
> 技術/程式術語保留英文；敘述以繁體中文（台灣用語）為主。
> 優先序 **P0（最嚴重／會損毀資料或掛死）→ P5（文件與 DX）**。
>
> 狀態圖示：`[ ]` 未處理 / `[~]` 進行中 / `[x]` 已完成。認領時請在項目後標註 `(@session-id, 日期)`。

---

## 第一輪（P0 ~ P5）— 已封存

第一輪（P0 最嚴重／會損毀資料或掛死 → P5 文件與 DX）全部項目已於 2026-07-16 ~ 2026-07-17 完成並合併進
`main`（P0/P1：PR #3；P2/P3-4：PR #6；P4/P5：分支 `feature/p4-p5-quality-and-docs` 與
`docs/reorganize-ai-human-audience`）。完整內容（每項的問題／位置／建議做法／測試細節）依 R2-15
（見下方「R2-P6」段落）的歸檔規則搬移至 [`todolist-archive/round-1-p0-p5.md`](todolist-archive/round-1-p0-p5.md)，僅供查閱歷史脈絡。

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

- [x] **R2-6 `_fetch_total_size` 不檢查 HTTP status → 錯誤頁的 Content-Length 被當成檔案大小**（2026-07-17 完成；測試：`tests/test_downloader_timeouts.py::TestSizeDiscoveryRejectsNonSuccessStatus`。實作：HEAD 回應非 2xx（`not head_response.ok`）時立即丟 `RuntimeError`（訊息含 status code 與「download URL 可能已過期或被封鎖」提示），不再讀取錯誤頁的 `Content-Length`。）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_fetch_total_size`）
  - 問題：HEAD 回 403/429/5xx 時仍讀 `Content-Length`（錯誤頁大小），切出完全錯誤的 ranges，之後每個 chunk 都 size mismatch，浪費整輪重試才失敗，且訊息不指向真正原因（URL 過期/被封鎖）。
  - 建議做法：非 2xx 直接丟 `RuntimeError`（訊息含 status code 與「download URL 可能已過期或被封鎖」提示）。
  - 測試：mock HEAD 回 403 驗證立即失敗且訊息正確。

## R2-P2 — 資源使用與死碼

- [x] **R2-7 每個 chunk 整段緩衝在記憶體 → 峰值可達數百 MiB**（2026-07-17 與 R2-13 一併完成，分支 `feature/r2-7-r2-13-streaming-resume`；測試：`tests/test_downloader_resume_and_streaming.py::TestChunkStreamsIncrementallyToTmpThenRenames`、`TestMergePartsStreamsInsteadOfBuffering`。實作：`_download_chunk` 改為邊收邊寫 `.partNN.tmp`（每次 write 後 `flush()` 讓磁碟即時可見），確認完整位元組數後才 `replace()` 成最終 `.partNN`（原子改名，Windows-safe）；失敗/例外路徑一律清掉未完成的 `.tmp`。`_merge_parts` 改用 `shutil.copyfileobj` 串流合併；排程迴圈重用分支原本 `part_path.read_bytes()` 只為取長度也一併改成 `part_path.stat().st_size`。）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_download_chunk` 的 `io.BytesIO`；`_merge_parts` 的一次性 `chunk.read()`）
  - 問題：預設 20 threads × ≥20MiB split ≈ 400MiB 峰值；媒體檢查失敗重試會把 split 加倍再翻倍。對打包成 exe 給一般 Windows 使用者的情境不友善。
  - 建議做法：chunk 改為邊下邊寫暫存檔（`.partNN.tmp` 完成後 rename 成 `.partNN`，rename 的原子性同時消除「寫一半的 part 被重用分支誤判完整」的風險）；`_merge_parts` 改 `shutil.copyfileobj` 串流合併。
  - 測試：驗證 rename 前的 `.tmp` 不會被排程重用分支撿走；合併結果 byte-identical。

- [x] **R2-8 死碼 `_load_cached_urls`**（2026-07-17 完成。實作：直接刪除該 method（確認全 repo 無呼叫端）；`url_cache_path` 建構參數旁補上 docstring 說明其僅為除錯／檢視用途（`download()` 每次執行都會先刪除舊檔、只寫入不讀回）。）
  - 位置：`src/k2s_downloader/core/downloader.py`（`_load_cached_urls`，全 repo 無呼叫端也無測試）
  - 問題：`download()` 開頭固定刪除 URL cache 檔再重建，`_load_cached_urls` 從未被呼叫 —— 「URL 快取重用」這個功能只做了寫入端。download URL 本身有時效，跨 session 重用價值本來就低。
  - 建議做法：直接刪除該 method（連同評估 `url_cache_path`/`urls.json` 是否還有存在必要 —— 若只剩除錯用途，在 docstring 註明）。

## R2-P3 — Windows exe 打包（PyInstaller）

- [x] **R2-9 exe 打包整備**（2026-07-17 完成。實作：① 新增 `Downloader.download(..., output_dir=None)`（經 `_apply_output_dir` 套用，只取解析後檔名的最後一段與 `output_dir` 組合，捨棄檔名自帶的目錄成分，CLI 從不傳入故行為不變）；新增 `gui/paths.py`（`app_data_dir()` 用 `QStandardPaths.AppDataLocation`、`default_download_dir()` 用 `QStandardPaths.DownloadLocation`），`gui/app.py::main()` 補上 `setOrganizationName`/`setApplicationName`（`QStandardPaths.AppDataLocation` 依賴這兩者才能解析出穩定路徑）；`gui/worker.py` 的 `DownloadWorker`/`ProxyLoaderWorker` 改用 `app_data_dir()` 下的 `tmp`/`urls.json`/`proxies.txt`，`DownloadWorker` 新增 `output_dir` 參數並轉呼叫 `download()`；`gui/main_window.py` 加「Save to」資料夾欄位＋Browse 按鈕（`QFileDialog.getExistingDirectory`），預設值為 `default_download_dir()`。② 新增 `k2s_gui.spec`（onedir、`console=False`、`icon='src/assets/icon/icon.ico'`、`datas` 對應 `resources/style.qss` → `resources`、`src/assets/icon/icon.ico` → `assets/icon`，符合 `_resource_path` 對 `_MEIPASS` 內相對路徑的期待）；只打包 `k2s_gui_entry.py`（CLI 入口的 `default_captcha_callback` 用 `Image.show()`+`input()`，windowed 模式無 stdin 會掛死，故不包）。③ `Readme.md` 新增「Building a Windows Executable」段落（含指令、SmartScreen 警告說明、ffmpeg 不隨附的提醒）。④ 本機以 `pip install PySide6 pyinstaller` 到專案 `.venv` 後實測：`pyinstaller k2s_gui.spec` 建置成功、`resources/style.qss`／`assets/icon/icon.ico` 確認落在 `dist/K2SDownloaderm/_internal/` 下（PyInstaller ≥6 的 onedir 佈局，`_MEIPASS` 對應到 `_internal/`）、實際執行 `K2SDownloaderm.exe` 確認程序常駐（非啟動即崩潰）後 `taskkill` 收尾；`git status` 確認 `dist/`/`build/` 未被追蹤（`.gitignore` 本來就排除）。測試：`tests/test_downloader_filename_and_paths.py::TestApplyOutputDir`（3 項，覆蓋 output_dir 為 None／join／捨棄檔名自帶目錄成分）；核心測試套件（126 項）全數通過。文件：`docs/ai/requirements.md`/`docs/human/requirements.md` 新增 AC-10.5／AC-10.6、補充 NFR-5；`docs/ai/architecture.md`/`docs/human/architecture.md` module map 加 `gui/paths.py`、補「檔案存放位置」段落說明 app-data dir 與 output_dir 的分工。
  - **未完成、有意識延後**：CI packaging job（原建議做法第 6 點的「加一個 CI job」半句）—— 本次以「本機手動建置＋實際啟動＋確認 process 常駐」驗證取代，未新增 GitHub Actions workflow（現有 `ci.yml` 只有 ubuntu runner、不含 PySide6/pyinstaller；要加 `windows-latest` job 且需要在真正有 Windows runner 的 CI 環境驗證過才能確保不是一個永遠紅燈的 job，本環境雖是 Windows 但屬於單次互動 session，不代表 GitHub Actions runner 的行為，故不在本輪新增，留給下次有 CI 存取權限時處理）。PySide6 excludes 體積優化（原第 5 點選配項）未做。

## R2-P4 — proxy 來源與生命週期管理（回應「proxyscrape 過時 proxy」問題）

- [~] **R2-10 proxy pool 品質改善**（2026-07-18 完成第 1~4 點；第 5 點使用者自備清單有意識延後，非本次範疇；第 6 點評估過不採用，無動作）。現況（改前）：單一來源 `api.proxyscrape.com`（**v1 舊版 API**，官方已遷移至 v2/v3，v1 隨時可能停止服務 —— 屆時 `fetch_remote` 拿到空清單、退化成純直連而無明顯錯誤）；驗證只打 `api.myip.com`；`proxies.txt` 快取無 TTL；`working_proxy_indexes` 只進不出。免費公開 proxy 本質就是高汰換率＋MITM 風險（見 P2-3），**換一家來源只能緩解、不能根治**，重點應放在驗證與生命週期：
  1. [x] **升級／多來源**：`src/k2s_downloader/core/proxy.py` 新增 `_PROXY_SOURCES`（proxyscrape v2 endpoint + `TheSpeedX/PROXY-List`、`monosans/proxy-list`、`proxifly/free-proxy-list` 三個 GitHub raw 清單），`_fetch_all_sources()` 用 `ThreadPoolExecutor` 並行抓取＋合併去重，任一來源例外（`RequestException`／格式錯誤等）只會透過 `status_callback` 回報一則訊息並跳過，不會擋住其他來源。`_normalize_candidate()` 順便處理 proxifly 清單帶 `scheme://` 前綴的格式。**已用真實網路驗證**（本機 `.venv` 安裝相依套件後實際執行）：4 個來源皆可連線、格式如預期（均為 `ip:port` 純文字，proxifly 需用 `proxies/protocols/http/data.txt` 子路徑取得純 HTTP 清單並去除 `http://` 前綴）。
  2. [x] **驗證目標對齊**：驗證方式從對 `api.myip.com` 發 GET 改為對 `PROXY_VALIDATION_URL = "https://k2s.cc/"` 發 HEAD（`session.head()`），且新增「非 2xx/3xx 視為驗證失敗」判斷（改前只要不丟例外就算通過，完全不管 status code）。**已用真實網路驗證**：對 300 個候選跑一次真實 refresh，47 個通過（約 16%），耗時約 62 秒，證實「對目標站實測」確實會篩掉泛用可達性檢查抓不到的失效 proxy。
  3. [x] **快取 TTL**：新增 `PROXY_CACHE_TTL_SECONDS = 12 * 60 * 60`（12 小時，落在建議的 6~24 小時區間）。`get_working_proxies()` 用 `cache_path.stat().st_mtime` 判斷快取年齡；一般呼叫（`refresh=False`、`recheck_cached=False`）若快取已過期，不再直接回傳，而是自動比照「建議做法」原文所說走既有的 `recheck_cached` 路徑（重新驗證既有清單，不是整個重新向所有來源抓取）。
  4. [x] **Runtime 降級**：新增 `Downloader._note_proxy_failure()`（`downloader.py`），由 `_mark_chunk_failed` 新增的 `proxy_idx` 參數觸發，以 `_proxy_consecutive_failures` dict（與 `working_proxy_indexes` 共用 `_working_proxy_lock`）追蹤連續失敗次數；達 `PROXY_FAILURE_EVICTION_THRESHOLD = 3` 即移出 `working_proxy_indexes`（不影響 `_acquire_proxy_lock` 隨機 fallback 層仍可能選到它；之後若成功會清除計數並重新加回清單，不是永久拉黑）。直連（index 0）不受影響（`_acquire_proxy_lock` 本就無條件優先嘗試它）。`refresh_proxies()` 換一批全新 proxy 清單時一併清空 `_proxy_consecutive_failures`（舊 index 對新清單無意義）。
  - **未完成、有意識延後**：第 5 點「使用者自備清單」（CLI flag／GUI 匯入自有 proxy）需要碰 CLI 參數解析與 GUI 檔案匯入 UI，是與品質改善不同性質的獨立功能，留待有需求時再立項。
  - **不採用**：第 6 點已評估 Tor／免費 VPN 兩個替代方案，維持不採用的結論。
  - 測試：`tests/test_proxy_pool_quality.py`（9 個，涵蓋多來源合併、單一來源失敗不擋其他來源、scheme 前綴正規化、驗證目標改對 k2s.cc、非 2xx 視為失敗、快取 TTL 新鮮/過期兩種路徑）；`tests/test_downloader_proxy_degradation.py`（9 個，涵蓋直連 index 0 永不追蹤、未達閾值不移除、達閾值移除、移除有 log、移除後仍可能被隨機 fallback 選中、`_mark_chunk_failed` 有無 `proxy_idx` 的差異、`refresh_proxies()` 重置狀態、成功後清除失敗計數）；既有 `tests/test_proxy_preference_and_cache.py` 三個測試因驗證方式從「不丟例外即成功」改為「不丟例外且 status_code < 400 才成功」而更新 mock（`.get` 改 `.head`、補上明確的 `status_code=200`）。全部 148 項測試通過，`ruff check .` 乾淨。
  - 文件：`AGENTS.md`、`Readme.md`（Security Note）、`docs/ai(+human)/architecture.md` §4（新增來源/驗證/降級三段說明＋2 個新常數的 timeout/retry 表格條目）、`docs/ai(+human)/requirements.md`（AC-6.3/AC-6.4 改寫、新增 AC-6.6）皆已同步更新。

## R2-P5 — 根本目的（突破 50KB/s）達成度檢視

- [~] **R2-11 用量測數據驗證加速機制、據此決定 proxy 架構去留**（2026-07-17 部分完成，分支 `feature/r2-11-throughput-telemetry-and-blocked-hint`——只做了「建議做法」第 1、3 點，第 2 點需要真實使用數據才能做決定，本環境無法連 Keep2Share 蒐集，留待有真實數據時再處理）
  - 已完成（第 1 點，telemetry）：`_report_progress` 新增 `is_direct: Optional[bool]` 參數，由 `_download_chunk` 依 `proxy_idx == 0` 傳入，分別累計 `_direct_bytes_downloaded`／`_proxy_bytes_downloaded`（排程重用分支接續已在磁碟上的區段時傳 `is_direct=None`，不計入這個統計，避免非即時連線的位元組污染量測）。新增 `_maybe_report_throughput()`，由 `_run_scheduling_loop` 每個 poll tick 呼叫，最多每 `TELEMETRY_REPORT_INTERVAL`（5 秒）透過既有 `status_callback` 回報一次「聚合速度／直連-proxy 百分比／活躍連線數」（CLI 印出、GUI log 面板本就會顯示，`core/` 未新增 print/logging）。測試：`tests/test_downloader_throughput_telemetry.py`（10 個，對修正前程式碼全部 fail）。
  - 已完成（第 3 點，零成本提示）：`ChunkDownloadFailed` 與 `k2s_client.py` 「所有 proxy 都被封鎖」的 `RuntimeError` 訊息都補上「若為動態 IP，可嘗試重啟數據機換 IP 後重試」的提示。
  - **未完成、暫緩**：第 2 點「依數據調整預設」需要真實 telemetry 數據才能決定（例如是否該把 proxy 改成 opt-in）——這正是本項的核心驗證/決策目的，但本環境無法連 Keep2Share 產生真實下載流量，只能等使用者用這次新增的 telemetry 實際跑過幾次大檔下載、觀察 log 訊息後再回來決定。第 4 點（aria2c 匯出）標記「選配」，未實作。
  - 文件：`docs/ai/architecture.md`／`docs/human/architecture.md` §4 補充 telemetry 設計說明。
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

- [x] **R2-13 可見且可靠的斷點續傳（使用者實際需求，2026-07-17 立項；2026-07-17 完成，分支 `feature/r2-7-r2-13-streaming-resume`）**
  - 測試：`tests/test_downloader_resume_and_streaming.py::TestResumeManifest`（manifest schema、成功後刪除、相符時跳過已完成區段、file_id 不符時拒絕續傳並清殘留檔、manifest 記錄已完成但實體檔案遺失時退回全新下載）。
  - 實作摘要：新增 `Downloader._manifest_path`/`_load_manifest`/`_persist_manifest`/`_prepare_resume`/`_clear_stale_part_files`；manifest（`<filename>.manifest.json`，flat 於 `tmp_dir`）記錄 file_id、total_size、split_size、split_count、各區段 range/bytes/downloaded，寫入用 `.tmp`+`replace()` 保證 atomic overwrite，並以專屬 `_manifest_lock` 序列化並行寫入。`_download_once` 新增 `file_id` 參數（`download()` 帶入真正的 file_id，其餘呼叫端預設空字串維持相容）；開始下載前呼叫 `_prepare_resume`：manifest 的 file_id/total_size/split layout 皆相符才信任其「已完成」標記，且仍會逐一對照磁碟上實際 part 檔大小再認定（manifest 只記錄意圖，磁碟檔案才是依據）；不符或缺席則呼叫 `_clear_stale_part_files` 清掉同檔名下所有殘留 part／manifest，避免不同來源的檔案因剛好同名同大小而被誤接續。成功合併後刪除 manifest；取消／永久失敗則保留（供下次續傳）。狀態訊息會回報「Resuming: found N/M segment(s)...」或「No previous progress found...」。
  - **範疇變更已同步**：`docs/ai/requirements.md`／`docs/human/requirements.md` 新增 REQ-11（含 AC-11.1~11.4），並更新 §2 Scope 的 in/out-of-scope 敘述與 AC-3.5/AC-3.6 措辭；另加 NFR-6 記錄串流寫入的有界記憶體特性。
  - 未完成的子項（有意識延後，非本次範疇）：GUI 顯示暫存目錄路徑＋「開啟資料夾」按鈕（R2-13 建議做法第 3 點的 UI 部分）尚未實作 —— 目前的可見性透過 `status_callback` 訊息（GUI 的 log 面板本就會顯示）與 manifest/part 檔本身在磁碟上可見達成；若要加開資料夾按鈕，需要碰 `gui/main_window.py`（無自動測試覆蓋，見 AGENTS.md 例外）。
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

## R2-P6 — 開發流程與文件維護（meta / tooling，非 core 程式碼）

- [x] **R2-14 AI review 留言常因篇幅過長被截斷 → 排在後面的建議看不到**（2026-07-17 完成；測試：`.github/scripts/test_ai_review.py::TestReviewPromptDoesNotAskForPraise`。實作：`build_review_prompt` 移除「如果是優秀的修改，請給予肯定」的指示，改為明確要求不寫「整體評價」／不讚美寫得好的部分，把篇幅留給 Critical/Improvement 清單，且每項建議只需精簡程式碼片段、不必整段重寫；system prompt 也補上「回覆力求精簡」。附帶發現：`.github/scripts/test_ai_review.py` 因 `pyproject.toml` 的 `testpaths = ["tests"]` 目前不在任何 CI 步驟中實際執行（`ai-review.yml` 只跑腳本本身、不跑 pytest），本次僅在本機手動安裝 `anthropic`/`PyGithub` 後執行驗證（8 passed），未動 CI 設定 —— 這是本項範疇外的既有缺口，未另立新項目。）
  - 位置：`.github/scripts/ai_review.py`（AGENTS.md §6 已提及此為已知限制）
  - 問題：目前的 review prompt 會產出「整體評價」段落，對已經寫得好的部分給予較多稱讚與情緒性描述，擠占留言篇幅；一旦留言逼近 GitHub 留言長度上限，排在後面的 Critical/Improvement 建議就會被硬生生截斷（PR #13 的 review 留言即在第 5 項建議中途被截斷）。使用者判斷：不需要過多情緒價值來讚美已經寫得很好的部分，聚焦在有疑問或給出建議的地方，應可省下不少篇幅。
  - 建議做法：調整 review prompt，要求輸出聚焦在「有疑問／有具體修改建議」的項目，整體評價維持最多一兩句摘要即可，不需要逐點讚美寫得好的地方；把省下的篇幅留給 Critical/Improvement 清單本身，降低被截斷的機率。可考慮額外加上「若本輪無 Critical/Improvement，直接說『無重大問題』」這類極簡收尾，避免為了填內容而灌水。
  - 測試：不易寫自動化測試（prompt 品質、輸出長度屬主觀判斷）；驗收方式建議是找一個過去曾被截斷的 PR diff 重跑一次 review，比對前後留言長度與「是否仍會截斷」。

- [x] **R2-15 `docs/ai/todolist.md` 只會累加、不會歸檔 → 檔案越肥大越浪費 token**（2026-07-17 完成。實作：新增 `docs/ai/todolist-archive/round-1-p0-p5.md`，將第一輪（P0~P5，全數 `[x]`）的完整內容原文搬移過去（含所有相對連結重新校正路徑深度並逐一驗證可解析），`docs/ai/todolist.md` 對應段落改成一段摘要＋連結，檔案從 357 行降到約 180 行。第二輪（R2）因尚有 R2-6、R2-8~R2-12 未完成，依規則暫不歸檔。同步更新 `AGENTS.md`（§5 補歸檔規則說明）、`CONTRIBUTING.md`（Tracking 段落補一句）、`docs/README.md`（todolist 條目補上 archive 連結）。）
  - 位置：`docs/ai/todolist.md`（目前已累積第一輪 P0~P5 全部完成項目＋第二輪 R2-1~R2-13，超過 300 行）
  - 問題：每輪 review 的項目（含已完成的）都留在同一份檔案裡，從未移出。依 `AGENTS.md` 的建議閱讀順序，AI agent 開工前要讀這份文件，檔案越肥大，每次開工／每次 review 讀取消耗的 token 越多，且早已完成、之後多半不會再被參考的舊項目持續佔用 context 空間。
  - 建議做法：
    1. 以「輪次」為單位分檔：`docs/ai/todolist.md` 只保留「目前這一輪尚未完成的項目」＋簡短的歷史輪次索引；某一整輪（例如第一輪 P0~P5、第二輪 R2-1~R2-13）全部項目都已 `[x]` 完成後，把該輪的完整內容（含每項的「問題／位置／建議做法／測試」細節）搬移到 `docs/ai/todolist-archive/`（例如 `round-1-p0-p5.md`、`round-2-r2.md`），`todolist.md` 對應段落只留一行摘要＋連結。
    2. 明確訂出「何時歸檔」的規則（例如：該輪全部項目皆 `[x]` 才整輪搬移，避免搬到一半又要挖回來；歸檔動作本身也走一次 minimal-diff PR）。
    3. 同步更新 `AGENTS.md`／`CONTRIBUTING.md` 的開工閱讀順序，補一句「若需要查閱已完成輪次的歷史脈絡，才去讀 `docs/ai/todolist-archive/`」，避免歸檔後反而漏讀重要的既有決策記錄（例如 P4-2 那段關於 `nonlocal stop` 是否冗餘的分析，之後若要再動 `_download_chunk` 仍有參考價值）。
  - 測試：純文件結構調整，不影響程式碼；驗收方式是歸檔後 `docs/ai/todolist.md` 行數／估計 token 數明顯下降，且逐項核對 archive 內容與原內容一致、沒有遺漏任何一項。

## R2-P7 — 可靠性：單一區段永久失敗不該讓整個下載直接放棄

- [x] **R2-16 `ChunkDownloadFailed` 後自動重跑整個下載 → 實測後判定方向錯誤，改為提高單一 chunk 的重試上限**（2026-07-18 使用者實際用打包出的 exe 下載大檔時觸發，立項、實作、實測後當天推翻重做）
  - **原始使用者痛點**：某個 chunk 重試 8 次全部透過壞掉的 proxy 逾時，整個下載直接失敗中止；使用者得自己手動重新點「Start download」才能繼續，且不清楚重新開始是否要整個檔案重下載一次。
  - **第一版做法（已推翻）**：`Downloader.download()` 拆成外層（重試整次下載，`MAX_DOWNLOAD_RETRIES = 3`）＋內層（既有媒體檢查重試）兩層迴圈；`ChunkDownloadFailed` 被外層攔截後重新解一次 captcha、重新取一批下載 URL、重跑（靠 R2-13 的續傳跳過已完成區段）。
  - **實測後發現的問題（使用者第一手回報）**：這個做法讓體驗明顯變差，而非變好。原因：①`k2s_client.generate_download_urls` 每次都要重新解 captcha，且會依序遍歷整個 `self.proxies` 清單找一個能用的 proxy 來換 `free_download_key`——R2-10 把候選來源擴充成多來源聯集後，這份清單變得又大又混雜大量已死的 proxy，導致「重新產生 URL」這個步驟本身就要跑很久、且每次都要使用者重新輸入一次 captcha。使用者原話：「原本的應用...只要能開始下載基本上都可以下載完，即使到最後速度變很慢，現在這樣常常跟我說 proxy 斷掉然後就切掉我又要重新 captcha...人不注意的時候根本不可能載完」。換句話說：把「整個下載」當成重試單位、還要求使用者互動（captcha），對「單一 chunk 運氣不好抽到爛 proxy」這種其實很常見的情況來說是不成比例的重手段。
  - **改用的做法**：完全還原 `download()` 到單一嘗試的原始結構（移除外層迴圈、`_generate_urls_for_attempt` 輔助方法、`MAX_DOWNLOAD_RETRIES` 常數），改成把 `MAX_CHUNK_RETRIES` 從 8 大幅調高到 25——同一個 chunk 在**同一個 session 內**（沿用已經取得的下載 URL，不需要新 captcha、不需要重新遍歷 proxy 清單）就能有更多機會換到能用的連線；再加上 R2-10 的降級機制（同一 proxy 連續失敗會被排除出優先清單），下載跑得越久，能用的 proxy 名單會越乾淨。真正到了 25 次都失敗（判斷是來源 IP 或整個 proxy pool 都被封鎖），才維持原本行為：整個下載中止、丟出 `ChunkDownloadFailed`，使用者自行決定要不要手動重試（重試時 R2-13 的續傳一樣會跳過已完成的部分）。
  - 測試：`tests/test_downloader_whole_download_retry.py` 改寫成單一項回歸測試，鎖定「`ChunkDownloadFailed` 只會呼叫一次 URL 產生（=一次 captcha），不會觸發第二次」，防止未來不小心又把這個已經被推翻的自動重試機制加回來。核心測試套件（149 項）全數通過，`ruff check .` 乾淨。
  - 文件：`docs/ai/requirements.md`／`docs/human/requirements.md` 的 AC-4.5 已移除、AC-4.1 改回描述單次下載中止並補充「為何不做整個下載層級的自動重試」；`docs/ai/architecture.md`／`docs/human/architecture.md` §2 控制流程圖還原成單層迴圈、§5 錯誤分類表與重試/backoff 參數表更新為 `MAX_CHUNK_RETRIES = 25`（移除 `MAX_DOWNLOAD_RETRIES` 條目）。
  - **給下一個 agent 的教訓**：這是一個「看起來合理但實測後被證明方向錯誤」的案例——加自動重試前，務必想清楚重試單位的成本（這裡的成本是「使用者互動」和「掃過整個 proxy 清單」），而不是只看「使用者不用手動重試」這個表面的好處。之後如果又有類似「下載失敗希望自動重試」的需求，優先考慮在不需要重新 captcha／不需要重新掃 proxy 清單的範圍內解決（例如提高既有計數上限），而不是重新走一次完整的 URL 產生流程。

---

## 建議處理順序

> 第一輪（P0 ~ P5）的處理順序與完成紀錄已隨該輪一併封存，見
> [`todolist-archive/round-1-p0-p5.md`](todolist-archive/round-1-p0-p5.md) 的「完成紀錄」段落。

第二輪（R2-1 ~ R2-16）：R2-1~R2-9、R2-13、R2-16 已完成；R2-10 完成第 1~4 點（第 5 點延後、第 6 點不
採用）；R2-11 部分完成（telemetry 與零成本提示已做，「依數據調整預設」需要真實使用數據才能決定，留待
之後）（見各項狀態與 PR 連結）。R2-12 仍未認領，建議等 R2-11 的 telemetry 累積到真實使用數據後再決定
要採用哪個尾端對策；其對策 4（縮小尾端 split）零架構改動可先行，不需要等數據。

**R2-P6（R2-14、R2-15）** 是 2026-07-17 使用者直接提出的兩項流程／文件維護改善（review 留言截斷問題、
todolist 歸檔機制），與上述 R2-1~R2-13 的程式碼修正屬不同性質；兩項皆已完成（見各項狀態）。本檔（含
`AGENTS.md`/`CONTRIBUTING.md`/`docs/README.md` 的對應段落）與 `.github/scripts/ai_review.py` 的變動即是
R2-15/R2-14 本身的成果。
