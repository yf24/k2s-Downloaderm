# K2S Downloader — 優化待辦清單 (todolist)

> 本檔提供給後續 session 接手用。每項含：**問題**、**位置**、**建議做法**、**狀態**。
> 技術/程式術語保留英文；敘述以繁體中文（台灣用語）為主。
> 優先序 **P0（最嚴重／會損毀資料或掛死）→ P5（文件與 DX）**。
>
> 狀態圖示：`[ ]` 未處理 / `[~]` 進行中 / `[x]` 已完成。認領時請在項目後標註 `(@session-id, 日期)`。

---

## P0 — 正確性缺陷（會造成資料損毀、掛死或誤殺 process）

- [ ] **P0-1 `parse_size` 的 IEC 單位換算錯誤**
  - 位置：`src/k2s_downloader/core/downloader.py:79`（`units` 對照表）
  - 問題：`KIB/MIB/GIB/TIB` 被定義成 10 的次方（`10**3, 10**6, ...`），但 KiB/MiB 應為 2 的次方。
    例如 `--split-size 20MiB` 會被算成 20,000,000 bytes；`5MiB` 更會低於 `download()` 的
    5 MiB 下限而直接丟出 `ValueError`。
  - 建議：`KIB=2**10, MIB=2**20, GIB=2**30, TIB=2**40`；同時補 `parse_size` 的邊界單元測試（見 P3-1）。

- [ ] **P0-2 `_build_ranges` 切段可能產生間隙／重疊 → 合併後檔案損毀**
  - 位置：`src/k2s_downloader/core/downloader.py:549`（`_build_ranges`）
  - 問題：每段的 `start` 與 `end` 各自獨立 `round()`，相鄰段之間不保證「前段 end+1 == 後段 start」，
    大檔在特定 `split_count` 下可能漏 1 byte 或重疊。
  - 建議：改用累進切法（`start = prev_end + 1`，最後一段 `end = total-1`），確保完整覆蓋且不重疊；
    以單元測試驗證「所有段 bytes 總和 == total_size 且連續」（見 P3-2）。

- [ ] **P0-3 proxy lock 的 TOCTOU 與 blocking `acquire` → 潛在死結／卡住**
  - 位置：`src/k2s_downloader/core/downloader.py:371`–`388`（`download_chunk` 內選 proxy）
  - 問題：`while proxy_locks[idx].locked(): 換一個` 為 check-then-act，兩個 thread 可同時通過檢查後，
    其中一個在 `proxy_locks[idx].acquire()`（預設 blocking）永久阻塞，且它此時仍持有 `url_lock` 不放。
    proxy 數量少而 threads 多時更容易發生。
  - 建議：改用 `acquire(blocking=False)` 的迴圈（搭配上限次數或短暫 sleep），取不到就放掉 `url_lock` 回排程。

- [ ] **P0-4 `working_proxy_indexes` 多執行緒無鎖寫入 → race condition**
  - 位置：`src/k2s_downloader/core/downloader.py:436`（`.append()`），讀取於 `:373`
  - 問題：多個 daemon threads 同時 `append`/讀取同一個 list，無鎖保護。
  - 建議：以既有的 `self._proxy_state_lock`（或新增鎖）保護；或改用 thread-safe 結構。

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

- [ ] **P1-1 移除未使用的 runtime 依賴 `aiohttp`**
  - 位置：`pyproject.toml:17`
  - 問題：整個程式碼庫從未 `import aiohttp`（已 grep 確認），卻列為 runtime dependency。
  - 建議：直接刪除，縮小安裝體積。

- [ ] **P1-2 `pyinstaller` 不應是 runtime 依賴**
  - 位置：`pyproject.toml:18`
  - 問題：打包工具被放進 `[project].dependencies`，一般使用者安裝會被迫拉入。
  - 建議：移到 optional group（例如 `[project.optional-dependencies].build`）或 dev。

- [ ] **P1-3 缺少 test / lint 的 CI**
  - 位置：`.github/workflows/`（目前只有 `ai-review.yml`）
  - 問題：沒有自動跑 `pytest` 與 `ruff` 的 workflow，回歸無法把關。
  - 建議：新增 `ci.yml`，於 push / PR 執行 `pytest -q` 與 `ruff check`。

- [ ] **P1-4 Readme 的「Legacy Entry Points」指向不存在的 `main.py`**
  - 位置：`Readme.md`（Legacy Entry Points 段）
  - 問題：宣稱 `python main.py` 仍可用，但專案無 `main.py`；實際入口為 `python -m k2s_downloader`
    （`src/k2s_downloader/__main__.py`）與 `k2s_gui_entry.py`。
  - 建議：更正為實際入口，或補上相容 shim。

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

- [ ] **P3-1 `parse_size` 邊界測試**（鎖住 P0-1 修正）
  - 涵蓋 `B/KB/MB/GB` 與 `KiB/MiB/GiB`、無單位、非法輸入。
- [ ] **P3-2 `_build_ranges` 連續性測試**（鎖住 P0-2 修正）
  - 驗證多種 `total_size` × `split_count`：各段連續、無重疊、bytes 總和等於 total。
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
- [ ] **P5-2 補齊 tooling 設定**：`pyproject.toml` 加入 `[tool.ruff]` / `[tool.pytest.ini_options]`，新增 `CONTRIBUTING`。
- [ ] **P5-3 Readme 補充**：proxy/captcha 實際行為、法律與使用聲明、`.[dev]` 安裝與測試說明。

---

## 建議處理順序

1. 先做 **P0-1、P0-2** 並同步補 **P3-1、P3-2**（純函式、好測、風險高）。
2. 再處理併發相關 **P0-3、P0-4** 與掛死相關 **P0-5、P0-6**。
3. 接著 **P1**（依賴／CI／文件入口）讓專案可持續驗證。
4. 之後依 P2 → P4 → P5 逐步推進。

> 註：以上為「檢視提案」，尚未動任何程式碼。接手 session 請逐項認領、附上測試，並在對應項目更新狀態。
