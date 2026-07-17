# 文件索引

本專案文件依「閱讀對象」分成兩份，不再散落於根目錄各自命名（例如舊有的 `readme-en.md`／`readme-zh.md`）。

## `docs/ai/` — AI agent 適用的 canonical 文件（英文撰寫）

- [`HANDOFF.md`](ai/HANDOFF.md) — 最新進度快照（動態內容，寫給接手的下一個 agent）
- [`requirements.md`](ai/requirements.md) — 需求文件（REQ / AC 逐項驗收標準）
- [`architecture.md`](ai/architecture.md) — 架構文件（module map、control flow、threading model、error taxonomy、timeout 一覽、CI）
- [`todolist.md`](ai/todolist.md) — 依優先序（P0～P5，第二輪起改用 R2-N）排列的問題追蹤清單／開發 backlog；已整輪完成的舊 backlog 封存於 [`todolist-archive/`](ai/todolist-archive/)

## `docs/human/` — 對應的人類可讀繁體中文版本

- [`requirements.md`](human/requirements.md)
- [`architecture.md`](human/architecture.md)

兩邊內容應保持同步；技術／程式名詞一律保留英文，僅敘述性文字語言不同。

## 其餘文件（維持在根目錄，GitHub 慣例位置，勿搬動）

- [`Readme.md`](../Readme.md) — 使用者安裝／使用說明（`pyproject.toml` 的 `readme` 欄位也指向此檔，不能搬動）
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — 貢獻／開發流程、code style、測試慣例、commit 格式
- [`AGENTS.md`](../AGENTS.md) — AI agent／Coder agent 進入本專案前的第一份文件（導覽 + 硬規則速查 + PR 工作流程）
