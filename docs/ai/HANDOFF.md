# K2S Downloader — Handoff

> 最新進度快照，寫給接手的下一個 agent（人類也可讀）。**動態內容，很快會過期** — 若本檔與 `git log`／GitHub 上實際的 PR/Issue 狀態衝突，一律以 `git log`／GitHub 現況為準。

## 現況（2026-07-17）

- `main` 分支目前與之前一樣，todolist（`docs/ai/todolist.md`）P0~P5 全數完成的狀態。
- 有一個尚未 merge 的 PR：**[#10](https://github.com/yf24/k2s-Downloaderm/pull/10)`docs/reorganize-ai-human-audience` → `main`**。把散落在根目錄的文件依受眾（AI／人類）重新分類，新增根目錄 `AGENTS.md` 作為 AI agent 進入點。目前 state=open，尚未 merge，PR 上目前沒有 review comment（截至本檔寫入時為止，`.github/workflows/ai-review.yml` 輸出的 review 留言尚未出現）。
- 本檔（`docs/ai/HANDOFF.md`）本身也是同一個 PR #10 的一部分（同分支上的追加 commit）。

## PR #10 具體做了什麼

（以 PR 描述本身最準，這裡只摘要）

- 新增 `docs/ai/{requirements,architecture,todolist}.md`（canonical、英文）與 `docs/human/{requirements,architecture}.md`（人類可讀、繁中），取代根目錄原本的 `requirements-en.md`／`readme-en.md`／`todolist.md`／`requirements-zh.md`／`readme-zh.md`。
- 新增 `docs/README.md`（分類索引）與根目錄 `AGENTS.md`（AI agent 進入點，含原 `.claudeprompt` 的 PR/review 三條規則）。
- 更新 `CONTRIBUTING.md`、`Readme.md` 的交叉連結。

## 已知限制／待辦（下一個 agent 可以接手的部分）

1. **舊路徑 stub 檔案尚未真正刪除。** `requirements-en.md`、`readme-en.md`、`todolist.md`、`requirements-zh.md`、`readme-zh.md`、`.claudeprompt` 這 6 個檔案目前內容只剩一兩行「已搬移至 X」的提示，物理上還留在根目錄。
   - **原因**：這次串接的 GitHub MCP 工具（`github-coder`）只曝露 `create_or_update_file` / `push_files`（GitHub Contents API，只能新增/更新），沒有曝露刪除端點，也沒有曝露底層 Git Data API（`create_tree`/`create_commit`/`update_ref`）可以繞過去做刪除。
   - **也試過本機 `git` CLI**：sandbox 內有 `git` 執行檔，可以匿名 `git clone`（唯讀，因為是公開 repo），但沒有任何 push 用的憑證 —— 沒有 `GITHUB_TOKEN`/`GH_TOKEN` 環境變數、沒有 `~/.netrc`、`git config --global` 是空的、`~/.ssh` 不存在、SSH 連線連 DNS 都解不出來（sandbox 網路限制）。所以本機 git push 這條路也是死路，不是沒試，是真的沒有可用憑證。
   - **下一步**：如果人類有這個 repo 的 push 權限，直接在自己電腦上跑：
     ```
     git rm requirements-en.md readme-en.md todolist.md requirements-zh.md readme-zh.md .claudeprompt
     git commit -m "chore: remove doc stubs superseded by docs/ai + docs/human reorg"
     git push
     ```
     或是到 GitHub 網頁介面對這 6 個檔案分別按「Delete file」。也可以在有 push 權限的環境（例如接了有寫入權杖的 `gh` CLI，或掘了 GitHub App/PAT 的 MCP 連接器）下，直接請下一個 agent 執行上面的 `git rm`。
2. **Claude.ai Project 的 custom instructions 尚未更新。** 這個專案在 Claude.ai 平台端另外設定了一份「K2S Downloader — Agent Instructions」的 custom instructions（不是 repo 檔案），內容彙整自舊版文件路徑（含一份已經不存在於 repo 的 `HANDOFF.md`）。這份設定活在平台端，這次工具鏈碰不到，需要人類手動到 Project 設定頁把它更新成指向新的 `AGENTS.md` / `docs/ai/` / `docs/human/` 路徑，或者乾脆整份刪掉、改成只留一句「詳見 repo 根目錄 `AGENTS.md`」。
3. **PR #10 尚未 review／merge。** 下一個 agent 若被要求「處理 review 意見」，依 `AGENTS.md` 第 6 節規則：讀取 PR 上的最新 AI reviewer 留言、只處理 Critical/嚴重 Improvement、忽略 nitpick、一輪對話只 push 一次，push 完就停止等人類 merge。

## 給下一個 agent 的建議起點

依 `AGENTS.md` 開工前建議閱讀順序：`AGENTS.md` → `docs/ai/HANDOFF.md`（本檔）→ `docs/ai/requirements.md` → `docs/ai/architecture.md` → `docs/ai/todolist.md` → `CONTRIBUTING.md`。若任務是「清理上面第 1、2 點的殘留」，先跟人類確認是否已取得可寫入 credential，再動作；純文件變動不需要跑 `pytest`/`ruff`，但若牵動 `src/`/`tests/`，記得照 `CONTRIBUTING.md` 的流程跑過再 push。
