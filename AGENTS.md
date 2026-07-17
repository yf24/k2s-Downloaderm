# K2S Downloader — Agent Instructions

> 本文件是 AI agent（含 Coder Agent／Reviewer）在 `yf24/k2s-Downloaderm` 專案內開始任何工作前，第一個要讀的檔案。內容彙整自 `docs/ai/` 下的 canonical 文件與本專案既有的開發慣例；細節請以連結出去的原始文件為準，本檔僅作導覽 + 硬規則速查。原本的 `.claudeprompt`（Coder Agent 的 PR/review 規則）已併入本檔第 6 節。

## 0. 建議閱讀順序（開工前）

1. 本文件（`AGENTS.md`）— 導覽 + 硬規則速查
2. [`docs/ai/requirements.md`](docs/ai/requirements.md) — canonical 需求文件（REQ/AC 逐項驗收標準）
3. [`docs/ai/architecture.md`](docs/ai/architecture.md) — canonical 架構文件（module map、control flow、threading model、error taxonomy）
4. [`docs/ai/todolist.md`](docs/ai/todolist.md) — 依優先序（P0～P5）排列的問題追蹤清單／開發 backlog
5. [`CONTRIBUTING.md`](CONTRIBUTING.md) — 開發流程、code style、測試慣例、commit 格式（人類與 agent 共用）

`docs/ai/` 是 AI 適用的 canonical 版本（英文撰寫）；`docs/human/` 是對應的人類可讀繁體中文版本，內容應與 `docs/ai/` 保持同步。`Readme.md`（根目錄，大寫 R）是使用者導向的安裝／使用說明，不是架構文件。完整文件索引見 [`docs/README.md`](docs/README.md)。

若你看到根目錄還留著 `readme-en.md`、`readme-zh.md`、`requirements-en.md`、`requirements-zh.md`、`todolist.md`、`.claudeprompt` 這幾個檔案，且內容只有一兩行「已搬移」提示，代表這些是 2026-07-17 文件整理後留下的轉址 stub（因當時串接的 GitHub 工具沒有刪除檔案的能力）。請直接依 stub 指向的新路徑閱讀，不要以舊檔內容為準；有能力的話歡迎直接刪除這些 stub 檔案。

## 1. 專案是什麼

一個 Keep2Share（`k2s.cc`）檔案的平行下載工具。把單一檔案切成多個 byte-range 區塊，並行下載（可選第三方 HTTPS proxy pool 規避單一 IP 速率限制），完成後重組成目標檔案。CLI（`k2s-downloader`）與 PySide6 桌面 GUI（`k2s-downloader-gui`）共用同一套 `core` 引擎。

**範疇外**（不要做）：批次／佇列下載多個 URL；Keep2Share 付費帳號存取；跨 process 續傳；上傳功能；自動解 captcha（captcha 一律交給使用者／呼叫端處理）。

## 2. 模組地圖（速覽版）

```
src/k2s_downloader/
├── cli.py                # argparse CLI 前端
├── core/                 # 無 GUI 依賴，可獨立 import／測試 — 商業邏輯唯一所在地
│   ├── downloader.py      # Downloader：整體協調、切分、排程、下載、合併
│   ├── k2s_client.py      # Keep2Share API client：captcha、URL 產生、檔名查詢
│   └── proxy.py           # 公開 proxy 清單抓取／驗證／快取
└── gui/                  # PySide6 前端，只透過 core/ 的 callback 介面互動
    ├── app.py / main_window.py / worker.py

tests/                    # 對應 core/ 各模組；所有 requests 呼叫皆 mock；無 GUI 測試
```

`core/` 是唯一含商業邏輯的套件；`cli.py` 與 `gui/` 都是薄前端。`Downloader.download()` 是前端唯一會呼叫的公開入口。完整模組地圖、control flow、threading model 見 [`docs/ai/architecture.md`](docs/ai/architecture.md)，這裡只保留最小速查版本，避免兩份文件內容漂移。

## 3. 動工前必須知道的硬規則

- **`core/` 不能依賴任何 GUI toolkit**（NFR-3）。UI 邏輯只能放 `gui/`。
- **每個對外 HTTP 呼叫都要有明確 timeout**（NFR-1），不可依賴 library 預設值。新增網路呼叫時務必命名常數（參考 `docs/ai/architecture.md` 的 Timeout inventory 表）。
- **多執行緒共用狀態一律要有專屬 lock 保護**（NFR-2）：`working_proxy_indexes`、`_active_proxy_indexes`、`_bytes_downloaded`/`_done_count`、`url_locks[i]`、`proxy_locks[i]`。取消狀態只用單一 `stop_event`（`threading.Event`），不要再額外鏡射 boolean（見 `docs/ai/architecture.md` §3 的說明，這是刻意的設計決策，過去踩過雷）。
- **`_acquire_proxy_lock` 絕不能用 blocking `.acquire()`**，一律 `acquire(blocking=False)` + 短 sleep 迴圈，且要在 `stop_event` 被設定時能提早退出（過去因 check-then-act race 導致死結）。
- **`core/` 只透過建構子 callback**（`status_callback`／`progress_callback`／`proxy_state_callback`）跟外界溝通，不可在 `core/` 內加 `print()`／`logging`。
- **proxy pool 是不可信通道**：來自 `proxyscrape.com`、只做輕量可達性驗證、proxy 這段連線本身是未加密明文 HTTP（即使目標是 HTTPS）。直連永遠優先，proxy 只是 fallback；改動這段邏輯前務必讀 `docs/ai/architecture.md` §4 與 `docs/ai/requirements.md` REQ-6。
- **例外分類要保持**（見 `docs/ai/architecture.md` §5）：`DownloadCancelled`（使用者取消，非錯誤）／`ChunkDownloadFailed`（區段重試耗盡）／`K2SFileNotFound`（可捕捉，不可 `sys.exit()` 殺掉整個 process）／`RuntimeError`（要附上可能原因，如 IP 被封鎖）／`ValueError`（輸入驗證，網路呼叫前就丟）。新增錯誤處理時比照這個分類，不要新增未分類的裸例外往外丟。

## 4. Code Style 與開發／測試指令

完整規則見 [`CONTRIBUTING.md`](CONTRIBUTING.md)（環境設定、`uv sync --extra dev`、`uv run pytest -q`、`uv run ruff check .`、commit message 格式、測試慣例）。這裡只強調兩條最容易被忽略的：

- 程式碼註解一律英文；敘述性文件（`Readme.md`、`docs/human/*.md`、`docs/ai/todolist.md`）預設繁體中文（台灣用語），技術／程式名詞保留英文。
- 修 bug 時做最小、針對性的 diff；`docs/ai/todolist.md` 用優先序（P0 最嚴重～P5 文件／DX）追蹤問題，不要把不相關的改動混進同一個 PR。

## 5. Tracking：`docs/ai/todolist.md`

這是本專案的 living backlog，依優先序排列（P0 最嚴重／會損毀資料或掛死 → P5 文件與 DX）。目前 P0～P5 皆已完成（狀態見檔案內 `[x]`）。若要接手新項目：

- 更新對應項目的 checkbox、完成日期、測試位置。
- 保留原本的「問題／位置／建議做法」文字，讓後續讀者仍看得懂脈絡。
- PR 盡量只涵蓋單一優先序的工作量，不要把不相關的修正綁在一起。

若 `docs/ai/todolist.md` 的優先級都已完成，代表需要重新從頭檢視現況（程式碼可能已有新變動）再開新一輪優先序評估，不要假設清單仍完整反映現狀。

## 6. PR / Review 相關約束（Coder Agent 專用規則，原 `.claudeprompt` 內容）

這些是本 repo 對負責寫程式／推送 PR 的 agent 的明確限制，務必遵守：

1. **自動讀取 PR 評論**：提交 PR 後如需依 review 修正，使用 `github-coder` 系列工具讀取該 PR 底下雲端 AI Reviewer 留下的最新評論。
2. **最小修改價值過濾**：只修正重大 bug／邏輯漏洞／效能缺陷／安全性問題；直接忽略排版、命名微調、無關緊要的註解等 nitpick，不要為這些重新 commit/push。
3. **停損點限制**：**一輪對話只能修改並 push 一次**。Push 完成後立即停止，**不要**主動再去讀取下一輪自動觸發的 review 留言；等人類下達下一步指令，由人類做最終 merge 把關。

另外，`.github/scripts/ai_review.py` 貼上的留言目前有已知的長度截斷問題；若同一段程式碼在連續幾輪 review 收到方向互相矛盾的建議，應直接跟人類確認是否停止繼續來回調整，而不是自行猜測。

## 7. 環境與語言規則

- 開發／測試環境：Windows + Python 3.13；`pyproject.toml` 宣告最低支援 Python 3.9，CI 也對此下限跑測試。
- 全域語言規則：預設回覆與文件語言為繁體中文（台灣用語），技術／程式名詞保留英文；`docs/ai/` 下的文件固定使用英文撰寫（AI-facing canonical），`docs/human/` 是對應的繁中版本。程式碼註解一律英文。
- `ffmpeg` 只有在要跑媒體完整性檢查（`ensure_media_check=True`）時才需要，不影響一般測試套件執行。

## 8. 目前狀態

本檔刻意不記錄動態的開發進度（例如「目前有哪個 PR 正在進行」），因為這類資訊很快就會過期、且與文件本身脫鉤。請直接查 `git log`、GitHub 上開啟中的 PR／Issue，或重新檢視 `docs/ai/todolist.md` 現況，取得當下最新狀態，不要假設任何靜態文件描述的狀態仍然成立。
