# K2S Downloader — 需求文件

> 人類可讀的繁體中文版本；AI 工具請改讀 canonical 版本 [docs/ai/requirements.md](../ai/requirements.md)。本 repo 閱讀順序：[AGENTS.md](../../AGENTS.md) → docs/ai/requirements.md → docs/ai/architecture.md → 其他文件。

## 1. 目的

一個針對 Keep2Share（`k2s.cc`）檔案連結的平行下載工具。把單一檔案切成多個 byte-range 區塊，並行下載（可選擇透過第三方 HTTPS proxy pool 來規避單一 IP 的速率限制），完成後重新組合成目標檔案。CLI（`k2s-downloader`）與 PySide6 桌面 GUI（`k2s-downloader-gui`）共用同一套 core 引擎。

## 2. 範疇

**範疇內**：依 Keep2Share 分享連結下載單一檔案；透過 captcha 驗證通過 Keep2Share 公開 API 授權；並行分段傳輸並具重試／backoff；可選的 proxy pool 以取得 IP 多樣性；下載後可選的 `ffmpeg` 媒體完整性檢查；當暫存目錄（part 檔＋相符的續傳 manifest）仍在磁碟上時，接續中斷的下載 — 無論是同一次執行內或程式重啟後（見 REQ-11）。

**範疇外**：單次執行下載多個 URL 的批次／佇列功能；Keep2Share 付費帳號等級的存取；暫存目錄已被刪除或從未產生過內容時的續傳（磁碟上沒有任何東西可接續）；任何 Keep2Share 上傳功能；繞過或自動化解 captcha（本工具是把 captcha 交給使用者／呼叫端處理，見 REQ-5）。

## 3. 角色

- **終端使用者** — 透過 CLI 或 GUI 執行下載。
- **Library 呼叫端** — 直接 import `k2s_downloader.core`（例如提供自訂 `captcha_callback` 做自動化），而非使用任一前端介面。
- **Keep2Share API**（`k2s.cc`）— 外部第三方服務，不受本專案控制；其 captcha／rate-limit／錯誤訊息格式視為固定介面，`core/k2s_client.py` 依此格式做 duck typing。
- **第三方 HTTPS proxy**（來自 `proxyscrape.com`）— 不可信、可選的基礎設施，安全性說明見 REQ-6。

## 4. 功能需求

### REQ-1：接受並驗證 Keep2Share URL
- **AC-1.1**：給定符合 `https?://(k2s.cc|keep2share.cc)/file/<id>...` 格式的 URL，能成功解析出 file ID。
- **AC-1.2**：給定不符合此格式的 URL，在任何網路呼叫發生前就丟出 `ValueError`。

### REQ-2：取得檔案顯示名稱與總大小
- **AC-2.1**：在任何區塊下載開始前，透過 Keep2Share 的 `getFilesInfo` API 取得原始檔名。
- **AC-2.2**：若呼叫端提供的自訂檔名沒有副檔名，會自動補上原始檔案的副檔名；若自帶副檔名則原樣使用。
- **AC-2.3**：切分前透過 HTTP `HEAD` 請求的 `Content-Length` header 取得總大小；若遇到負值（部分 CDN 的 32-bit 有號整數溢位）會自動加上 2³² 修正。
- **AC-2.4**：這兩個查詢過程中若網路失敗，會丟出說明「主機可能封鎖／限速」的 `RuntimeError`，而不是原始例外或無限期卡住（兩個呼叫都帶明確 timeout）。

### REQ-3：切分檔案為 byte range 並平行下載
- **AC-3.1**：檔案會被切成 `ceil(total_size / split_size)` 個區段（至少 1 個），彼此連續相鄰 — 對任何合法的 `(total_size, split_count)` 組合，不會有位元組被漏掉（間隙），也不會被兩個區段重複認領（重疊）。
- **AC-3.2**：最多 `threads` 個區段同時下載，各自在獨立的 thread 中執行，由固定大小的每-thread「url lock」pool 控管。
- **AC-3.3**：`split_size` 至少要 5 MiB；小於此值會在任何下載開始前丟出 `ValueError`。
- **AC-3.4**：下載完成的位元組數與預期大小不符（誤差超過 1 byte）視為失敗並重試，不會被靜默接受。
- **AC-3.5**：磁碟上已存在且大小符合預期的 `.partNNN` 檔案會直接重複使用、不重新下載。這是實際執行續傳的底層機制；何時可信任這個機制由 REQ-11 規範（同一次執行內的重試永遠可信任；全新程式重啟後的嘗試則需要相符的續傳 manifest 才會被採信）。
- **AC-3.6**：成功後所有 part 檔案會依區段順序串接成最終目標檔案（透過 `shutil.copyfileobj` 串流寫入，不會整份讀進記憶體），並刪除暫存 part 檔；目標路徑上若已有舊檔會被覆寫。

### REQ-4：有界重試與 backoff，而非無限期卡住
- **AC-4.1**：失敗的區段（非 2xx 狀態碼、網路層例外、或位元組數不符）會以指數 backoff（基準 1 秒、上限 30 秒）重試，最多固定次數（8 次），超過後整個下載會中止並丟出 `ChunkDownloadFailed`，內含區段編號、嘗試次數與最後一次失敗原因。
- **AC-4.2**：URL 產生階段（任何區塊下載開始前的 captcha／proxy 探測階段）有自己的有界重試：captcha 答錯最多 3 次；同一個 proxy 連續 3 輪拿不到任何新 URL 就換下一個；所有 proxy 都試過仍失敗，會丟出說明「IP／proxy pool 疑似被封鎖」的 `RuntimeError`，而不是卡住不動。
- **AC-4.3**：本專案發出的每一個對外 HTTP 請求都帶有明確 timeout；沒有任何請求可能因對方主機無回應而無限期阻塞。
- **AC-4.4**：單一區塊下載 thread 內的失敗，一律經由同一套失敗紀錄流程處理（絕不被靜默吞掉），且絕不會讓該區段的「使用中」旗標卡住 — 否則排程迴圈會永遠卡在該區段上。

### REQ-5：Captcha 處理
- **AC-5.1**：在產生任何下載 URL 之前，會先取得 captcha 挑戰並交給呼叫端提供的 `captcha_callback(image_bytes, challenge, captcha_url) -> str`；預設實作（CLI 使用）會開啟圖片並從 stdin 讀取回應。GUI 提供自己的 callback，會透過 Qt signal 往返阻塞背景 worker thread，直到使用者在介面上送出回應。
- **AC-5.2**：captcha 答錯會觸發新的挑戰並再次呼叫 callback，最多累計 `MAX_CAPTCHA_ATTEMPTS`（3）次，超過後丟出 `RuntimeError`，說明答案可能是對的但 IP 疑似被封鎖。
- **AC-5.3**：Keep2Share 回應「File not found」時會丟出可被捕捉的 `K2SFileNotFound` 例外，而不是終止整個 process — 確保錯誤連結不會把 GUI 一起拖垮。

### REQ-6：區塊下載的可選 proxy pool
- **AC-6.1**：`Downloader.refresh_proxies()` 產生的 proxy 清單第一個元素恆為 `None`（代表「直連，不用 proxy」）；這個不變量在其他地方會被依賴（見 AC-6.2），且由 `get_working_proxies()` 的每一條回傳路徑保證成立。
- **AC-6.2**：為區塊選擇連線方式時，只要直連的槽位是空的就一律優先嘗試；只有在直連槽位忙碌／不可用時才會改用第三方 proxy。
- **AC-6.3**：Proxy 候選清單來自公開、未經驗證的第三方來源（proxyscrape.com），僅做輕量驗證（對 `api.myip.com` 做一次可達性測試）；這一點會明確告知使用者屬於 MITM 風險（proxy 這段連線本身是未經驗證的明文 HTTP，即使目標網址是 HTTPS），而非當成可信賴的通道。
- **AC-6.4**：Proxy 候選清單的快取路徑可設定（`Downloader(proxy_cache_path=...)` / `get_working_proxies(cache_path=...)`），預設為目前工作目錄下的 `proxies.txt` 以維持向後相容；寫入尚不存在的巢狀路徑時會自動建立父目錄。
- **AC-6.5**：多個並行的區塊下載 thread 在選擇 proxy 時，絕不會有兩個 thread 同時持有同一個 proxy；等待 proxy 槽位的 thread 一旦下載被取消就會盡快放棄（不會無限期忙等）。

### REQ-7：取消下載
- **AC-7.1**：在 `download()` 執行期間任何時間點呼叫 `Downloader.cancel()`（包含解 captcha、產生 URL、或下載區塊的過程中）都會讓該次呼叫丟出 `DownloadCancelled`，而不是繼續跑完或卡住。
- **AC-7.2**：在 `download()` 呼叫「之前」就設定的取消旗標不會殘留影響這次呼叫（旗標會在 `download()` 一開始、任何網路活動之前就被清除，避免上一次執行殘留的取消狀態讓這次呼叫悄悄變成空操作）。

### REQ-8：下載後可選的媒體完整性檢查
- **AC-8.1**：若啟用 `ensure_media_check`（預設開啟）、目標副檔名屬於已知媒體類型、且 `PATH` 上找得到 `ffmpeg`，會用 `ffmpeg -c copy -f null` 檢查下載結果是否損毀。
- **AC-8.2**：偵測到損毀時，會自動用兩倍原始 split size 重新下載一次；再次失敗就放棄並記錄「檔案仍然損毀」— 不會無限重試。
- **AC-8.3**：若 `ffmpeg` 不存在、副檔名不屬於已知媒體類型、或該檢查被關閉，下載會直接完成（不視為錯誤）。

### REQ-9：CLI 介面
- **AC-9.1**：`k2s-downloader <url> [--filename NAME] [--threads N] [--split-size SIZE] [--no-ffmpeg-check]` 會完整跑完一次下載，或以明確的非零 exit code 失敗。
- **AC-9.2**：`--split-size` 接受不分大小寫的二進位（IEC）單位後綴 — 純 `B`、單字母 `K`/`M`/`G`/`T`、雙字母 `KB`/`MB`/`GB`/`TB`、三字母 `KIB`/`MIB`/`GIB`/`TIB` — 全部以 1024 為底、非 1000；無法辨識的後綴會丟出面向使用者的錯誤（argparse `error()`），絕不是未捕捉的例外。
- **AC-9.3**：CLI 自己的 `--split-size` 預設值（`"20M"`）本身必須是同一套解析邏輯的合法輸入（曾經的迴歸問題：這點原本不成立，導致每次使用預設值都會直接崩潰）。

### REQ-10：GUI 介面
- **AC-10.1**：GUI 提供與 CLI 相同的下載參數（URL、自訂檔名、thread 數、split size、媒體檢查開關），外加 proxy pool 相關控制項（重新整理、重新驗證快取、候選數量上限）與顯示即時 proxy 可用狀態的開發者面板。
- **AC-10.2**：背景下載 thread 傳來的進度、狀態訊息、proxy 狀態更新，在送到 UI thread 前都會先節流（固定的 tick 間隔），避免高 thread 數造成過量 signal 流量把 UI 卡死。
- **AC-10.3**：需要 captcha 時會直接嵌入顯示在 GUI 內（圖片＋文字輸入框），而非卡在終端機提示；送出後會解除背景下載 thread 的阻塞。
- **AC-10.4**：下載進行中關閉主視窗會觸發取消，並在有限時間內等待 worker thread 停止後才讓 process 結束。

### REQ-11：透過磁碟上的 manifest 實現斷點續傳
- **AC-11.1**：每次下載都會維護一份續傳 manifest（暫存目錄下的 `<filename>.manifest.json`），記錄 Keep2Share 的 file ID、總大小、split size、split 數量，以及每個區段的完成狀態；區段完成時會即時更新，合併成功後會被刪除（檔案已完整組出來，就沒有東西需要續傳了）。
- **AC-11.2**：若暫存目錄裡已有一份與這次執行的 file ID、總大小、split 佈局都相符的 manifest，就會進入續傳模式：先前已完成的區段不會重新發出網路請求 — 但仍會先獨立重新驗證對應的 part 檔案確實存在於磁碟上且大小符合預期（manifest 記錄的是「意圖」，磁碟上的檔案才是最終依據），並會有狀態訊息回報已經有多少區段／位元組是先前留下的。
- **AC-11.3**：manifest 不存在、或其 file ID／總大小／split 佈局與本次執行不符時，絕不會被當成「可以重複使用剛好同名且位元組數吻合的殘留 part 檔」的依據（例如另一個 Keep2Share 連結恰好解析出相同的輸出檔名）；遇到這種情況會改為清除這些殘留的 part／manifest 檔案，並全新開始下載。
- **AC-11.4**：取消或永久失敗的下載，會保留其 manifest 與已完成的 part 檔案以供之後續傳；只有完全成功合併後才會刪除它們。

## 5. 非功能需求

- **NFR-1（Timeout）**：每一個對外網路呼叫都有明確命名的 timeout 常數（見 [docs/ai/architecture.md](../ai/architecture.md) 的「Timeout inventory」），不依賴 library 預設值。
- **NFR-2（並發安全）**：多個區塊下載 thread 共用的可變狀態（已知可用 proxy 清單、活躍 proxy 集合、進度計數器）都由專屬的 lock 保護；沒有任何欄位在無 lock 保護下被多個 thread 讀取-修改-寫入。
- **NFR-3（可攜性）**：`core/` 不依賴任何 GUI toolkit；必須能在未安裝 PySide6 的環境下獨立 import 與測試。
- **NFR-4（測試覆蓋）**：每個 bug 修復都要附上對應的迴歸測試（放在 `tests/`），該測試在修復前的程式碼上要能重現失敗。`gui/` 是唯一有記載的例外（`# pragma: no cover - GUI wiring`）。
- **NFR-5（平台）**：開發與測試環境為 Windows + Python 3.13；`pyproject.toml` 宣告最低支援 Python 3.9，CI 也會針對這個下限版本執行。
- **NFR-6（區塊記憶體用量有界）**：區段資料一邊接收一邊直接串流寫入其 `.partNNN.tmp` 檔（不會整段緩衝在記憶體中），且每次寫入後都會 flush，讓磁碟上的檔案漸進成長可見，而不是等到整段下載完才出現；重新命名成最終 `.partNNN` 名稱的動作是原子性的，且只在確認完整位元組數後才執行，因此下載到一半或中斷的嘗試絕不會被誤判為已完成的區段。

## 6. 目前已知限制

- Proxy pool 的信任模型本質上是「盡力而為、非安全通道」（見 AC-6.3）— 這是有意識的取捨，不是缺陷，但代表本工具不適合用在需要 proxy 這段連線具備機密性／完整性保證的下載場景。
- `--threads` / GUI 的 thread 數不會自動被 Keep2Share 願意核發的 URL 數量上限所限制；若可用 URL 數少於要求的 thread 數，實際 thread 數會被靜默調降（AC-3.2 仍然成立，只是對調降後的數字成立），並會有狀態訊息說明此調降。
- 沒有持久化的下載佇列／歷史紀錄；每次執行都是獨立、單次的下載。
