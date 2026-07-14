import os
from github import Github
from anthropic import Anthropic
# 1. 初始化 API 客戶端
github_client = Github(os.environ["GITHUB_TOKEN"])
anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
repo = github_client.get_repo(os.environ["REPO_NAME"])
pr = repo.get_pull(int(os.environ["PR_NUMBER"]))
# 2. 獲取這個 PR 的程式碼變動 (Diff)
comparison = repo.compare(pr.base.sha, pr.head.sha)
diff_text = ""
for file in comparison.files:
    diff_text += f"File: {file.filename}\n"
    diff_text += f"Patch:\n{file.patch}\n\n"
if not diff_text:
    print("沒有偵測到任何代碼變動。")
    exit(0)
# 3. 呼叫 Claude 進行審查
prompt = f"""
你是一位資深的 Python 技術主管。請針對以下 Pull Request 的代碼變動（Git Diff）進行嚴格的 Code Review。
請特別注意：
1. 是否有邏輯漏洞或潛在的 Bug？
2. 程式碼效能與可讀性是否有優化空間？
3. 是否符合 Python 的最佳實踐 (PEP 8)？
如果是優秀的修改，請給予肯定。如果有需要改進的地方，請具體指出並給出修改建議程式碼。
以下是程式碼變動：
{diff_text}
"""
print("正在發送給 Claude 審查...")
response = anthropic_client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=2000,
    temperature=0.2,
    system="你是一位嚴格、專業但語氣友善的 Code Review 機器人。請用繁體中文回覆。",
    messages=[{"role": "user", "content": prompt}]
)
review_comment = response.content[0].text
# 4. 將審查結果寫回 GitHub PR 的留言中
pr.create_issue_comment(review_comment)
print("Review 成功發表於 PR 頁面！")
