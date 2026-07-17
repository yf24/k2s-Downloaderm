"""AI 驅動的 Pull Request Code Review 腳本。

流程：讀取必要的環境變數（GITHUB_TOKEN、ANTHROPIC_API_KEY、REPO_NAME、
PR_NUMBER）、取得指定 PR 的程式碼變動（diff），呼叫 Claude API 進行審查，
最後將審查結果留言回該 PR。
"""
import os

from anthropic import Anthropic
from github import Github

# 限制使用的 AI 模型為 Claude Haiku 4.5（claude-3-5-haiku-20241022 已於
# 2026-02-19 下架），避免因設定被誤改而呼叫到成本更高或尚未驗證過的模型。
ALLOWED_MODEL = "claude-haiku-4-5"


def get_required_env(name: str) -> str:
    """讀取必要的環境變數，若缺少或為空字串則印出明確錯誤訊息並中止程式。

    Args:
        name: 環境變數名稱。

    Returns:
        該環境變數的值。

    Raises:
        SystemExit: 當環境變數未設定或為空字串時。
    """
    value = os.environ.get(name)
    if not value:
        print(f"錯誤：缺少必要的環境變數 {name}，請確認 workflow 是否已正確傳入。")
        raise SystemExit(1)
    return value


def build_review_prompt(diff_text: str) -> str:
    """組合要送給 Claude 的 code review prompt。

    留言篇幅設計上明確要求精簡、聚焦在問題本身（見下方 prompt 內文），
    而非對已經寫得好的部分展開讚美 —— 過去的版本會先寫一段「整體評價」
    肯定寫得好的地方，等真正進入 Critical/Improvement 清單時，篇幅已經
    所剩無幾，導致留言在 GitHub 留言長度上限前被截斷、後面的建議完全
    看不到（PR #13 即是一例，回應時第 5 項建議中途被截斷）。
    """
    return f"""
你是一位資深的 Python 技術主管。請針對以下 Pull Request 的代碼變動（Git Diff）進行嚴格的 Code Review。
請特別注意：
1. 是否有邏輯漏洞或潛在的 Bug？
2. 程式碼效能與可讀性是否有優化空間？
3. 是否符合 Python 的最佳實踐 (PEP 8)？

當你進行 Review 時，請將問題分類為 `[Critical/Bug]`（嚴重錯誤/漏洞）、`[Improvement]`（建議優化）與 `[Nitpick]`（挑剔/細節美化）。

* 如果只有 `Nitpick` 等級的問題，請直接給予 Approve，不要要求 Coder 重新修改。
* 只有在偵測到 `Critical` 或嚴重的 `Improvement` 時，才要求重新修改。

留言篇幅有限，請把篇幅都留給實際的問題與建議，不要花篇幅寫「整體評價」或讚美已經寫得好的部分：
* 不需要摘要或肯定程式碼寫得好的地方；如果整體品質不錯，最多一句話帶過即可（例如「其餘部分未發現重大問題」），不要展開描述好在哪裡。
* 只詳細說明有疑慮、需要修改的地方；每項請包含檔案位置與問題說明，修改建議以精簡的程式碼片段示意即可，不需要整段重寫。
* 若完全沒有 `Critical` 或 `Improvement` 等級的問題，直接回覆「本次變更無重大問題，Approve」，不需要其他內容。

以下是程式碼變動：
{diff_text}
"""


def get_pr_diff_text(repo, pr) -> str:
    """取得 PR 的完整 diff 文字內容。"""
    comparison = repo.compare(pr.base.sha, pr.head.sha)
    diff_text = ""
    for file in comparison.files:
        diff_text += f"File: {file.filename}\n"
        diff_text += f"Patch:\n{file.patch}\n\n"
    return diff_text


def main() -> None:
    # 1. 讀取並驗證必要的環境變數
    github_token = get_required_env("GITHUB_TOKEN")
    anthropic_api_key = get_required_env("ANTHROPIC_API_KEY")
    repo_name = get_required_env("REPO_NAME")
    pr_number = get_required_env("PR_NUMBER")

    # 2. 初始化 API 客戶端
    github_client = Github(github_token)
    anthropic_client = Anthropic(api_key=anthropic_api_key)
    repo = github_client.get_repo(repo_name)
    pr = repo.get_pull(int(pr_number))

    # 3. 獲取這個 PR 的程式碼變動 (Diff)
    diff_text = get_pr_diff_text(repo, pr)
    if not diff_text:
        print("沒有偵測到任何代碼變動。")
        return

    # 4. 呼叫 Claude 進行審查（模型限制為 ALLOWED_MODEL）
    prompt = build_review_prompt(diff_text)
    print("正在發送給 Claude 審查...")
    response = anthropic_client.messages.create(
        model=ALLOWED_MODEL,
        max_tokens=2000,
        temperature=0.2,
        system="你是一位嚴格、專業但語氣友善的 Code Review 機器人。請用繁體中文回覆，回覆力求精簡，把篇幅留給實際的問題與建議。",
        messages=[{"role": "user", "content": prompt}],
    )
    review_comment = response.content[0].text

    # 5. 將審查結果寫回 GitHub PR 的留言中
    pr.create_issue_comment(review_comment)
    print("Review 成功發表於 PR 頁面！")


if __name__ == "__main__":
    main()
