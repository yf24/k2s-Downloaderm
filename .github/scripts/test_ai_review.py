"""Unit tests for .github/scripts/ai_review.py.

只測試不需要真正呼叫 GitHub / Anthropic API 的純邏輯部分：
必要環境變數的讀取行為，以及 AI 模型是否被限制在 claude-haiku-4-5。

若執行環境未安裝 anthropic / PyGithub（這兩個套件只在 CI 的
ai-review workflow 中安裝，不屬於本專案的主要相依套件），
測試會自動略過，不會影響主專案的 pytest 執行結果。
"""
from __future__ import annotations

import importlib.util
import os
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
    def test_allowed_model_is_claude_haiku_4_5(self):
        # Exact match, not startswith: a startswith check would also pass
        # for an unintended variant like "claude-haiku-4-5-extended".
        assert ai_review.ALLOWED_MODEL == "claude-haiku-4-5"
