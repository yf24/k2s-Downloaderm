"""Unit tests for .github/scripts/ai_review.py.

只測試不需要真正呼叫 GitHub / Anthropic API 的純邏輯部分：
必要環境變數的讀取行為，以及 AI 模型是否被限制在 claude-haiku-4-5。

若執行環境未安裝 anthropic / PyGithub（這兩個套件只在 CI 的
ai-review workflow 中安裝，不屬於本專案的主要相依套件），
測試會自動略過，不會影響主專案的 pytest 執行結果。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("github")

SCRIPT_PATH = Path(__file__).resolve().parent / "ai_review.py"


def _load_ai_review_module():
    spec = importlib.util.spec_from_file_location("ai_review", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ai_review = _load_ai_review_module()


class TestGetRequiredEnv:
    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("FOO_TEST_VAR", "bar")
        assert ai_review.get_required_env("FOO_TEST_VAR") == "bar"

    def test_exits_when_missing(self, monkeypatch):
        monkeypatch.delenv("MISSING_TEST_VAR", raising=False)
        with pytest.raises(SystemExit):
            ai_review.get_required_env("MISSING_TEST_VAR")

    def test_exits_when_empty_string(self, monkeypatch):
        monkeypatch.setenv("EMPTY_TEST_VAR", "")
        with pytest.raises(SystemExit):
            ai_review.get_required_env("EMPTY_TEST_VAR")


class TestModelRestriction:
    # Explicit allow-list of exact model IDs, not a startswith check: a
    # startswith check would also pass for an unintended variant like
    # "claude-haiku-4-5-extended".
    ALLOWED_MODELS = {"claude-haiku-4-5"}

    def test_allowed_model_is_claude_haiku_4_5(self):
        assert ai_review.ALLOWED_MODEL in self.ALLOWED_MODELS


class TestReviewPromptDoesNotAskForPraise:
    """R2-14: the prompt used to explicitly ask the model to praise good
    changes ("如果是優秀的修改，請給予肯定"), which produced a lengthy
    "整體評價" preamble before the actual Critical/Improvement findings.
    Combined with a fixed max_tokens budget, this pushed later findings
    past the truncation point (observed on PR #13). The prompt must
    instead tell the model to skip praise and keep findings concise.
    """

    def test_prompt_does_not_instruct_the_model_to_praise_good_code(self):
        prompt = ai_review.build_review_prompt("--- diff ---")
        assert "請給予肯定" not in prompt

    def test_prompt_instructs_skipping_the_overall_assessment_preamble(self):
        prompt = ai_review.build_review_prompt("--- diff ---")
        assert "整體評價" in prompt  # only mentioned to say "don't write one"
        assert "不要花篇幅寫" in prompt or "不需要摘要或肯定" in prompt

    def test_prompt_still_contains_the_diff_text(self):
        prompt = ai_review.build_review_prompt("MARKER-DIFF-CONTENT")
        assert "MARKER-DIFF-CONTENT" in prompt

    def test_prompt_still_defines_the_severity_categories(self):
        prompt = ai_review.build_review_prompt("--- diff ---")
        assert "[Critical/Bug]" in prompt
        assert "[Improvement]" in prompt
        assert "[Nitpick]" in prompt
