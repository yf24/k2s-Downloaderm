"""Tests for P0-1: every outbound request in k2s_client must carry a timeout.

Without a timeout, a blocked/black-holed IP causes ``requests`` to hang
indefinitely, which is the root cause of the app appearing to freeze.
These tests assert that ``timeout`` is always passed, and that
``generate_from_key`` gives up after a bounded number of retries instead of
looping forever.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from k2s_downloader.core import k2s_client


def _mock_response(json_data):
    mock = MagicMock()
    mock.json.return_value = json_data
    mock.content = b"fake-image-bytes"
    return mock


class TestTimeoutsArePassed:
    def test_fetch_captcha_uses_timeout(self):
        with patch.object(k2s_client.requests, "post", return_value=_mock_response({"challenge": "c", "captcha_url": "u"})) as mock_post:
            k2s_client.fetch_captcha()

        _, kwargs = mock_post.call_args
        assert kwargs.get("timeout") == k2s_client.DEFAULT_TIMEOUT

    def test_get_name_uses_timeout(self):
        payload = {"files": [{"name": "movie.mp4"}]}
        with patch.object(k2s_client.requests, "post", return_value=_mock_response(payload)) as mock_post:
            name = k2s_client.get_name("file123")

        assert name == "movie.mp4"
        _, kwargs = mock_post.call_args
        assert kwargs.get("timeout") == k2s_client.DEFAULT_TIMEOUT

    def test_generate_from_key_uses_timeout_and_returns_url(self):
        with patch.object(k2s_client.requests, "post", return_value=_mock_response({"url": "https://example.com/f"})) as mock_post:
            url = k2s_client.generate_from_key("file123", "key123", proxy=None)

        assert url == "https://example.com/f"
        _, kwargs = mock_post.call_args
        assert kwargs.get("timeout") == k2s_client.DEFAULT_TIMEOUT


class TestGenerateFromKeyRetryBound:
    def test_gives_up_after_max_retries_instead_of_hanging_forever(self):
        with patch.object(k2s_client.requests, "post", side_effect=requests.exceptions.Timeout("boom")) as mock_post, \
             patch.object(k2s_client.time, "sleep", return_value=None):  # don't actually wait during the test
            with pytest.raises(RuntimeError):
                k2s_client.generate_from_key("file123", "key123", proxy=None, max_retries=3)

        assert mock_post.call_count == 3

    def test_recovers_after_a_transient_failure(self):
        good = _mock_response({"url": "https://example.com/f"})
        with patch.object(
            k2s_client.requests,
            "post",
            side_effect=[requests.exceptions.ConnectionError("blocked"), good],
        ) as mock_post, patch.object(k2s_client.time, "sleep", return_value=None):
            url = k2s_client.generate_from_key("file123", "key123", proxy=None, max_retries=3)

        assert url == "https://example.com/f"
        assert mock_post.call_count == 2

    def test_does_not_hang_wall_clock_time_when_mocked_fast(self):
        # Guard against accidental real sleeps sneaking back in: with time.sleep
        # patched out, the whole retry loop must finish near-instantly.
        with patch.object(k2s_client.requests, "post", side_effect=requests.exceptions.Timeout("boom")), \
             patch.object(k2s_client.time, "sleep", return_value=None):
            start = time.monotonic()
            with pytest.raises(RuntimeError):
                k2s_client.generate_from_key("file123", "key123", proxy=None, max_retries=5)
            elapsed = time.monotonic() - start

        assert elapsed < 1.0


class TestGenerateDownloadUrlsCaptchaImageTimeout:
    def test_captcha_image_get_uses_timeout(self):
        captcha_payload = {"challenge": "c", "captcha_url": "https://k2s.cc/captcha.png"}
        get_url_payload = {"free_download_key": "key123"}

        def fake_post(url, **kwargs):
            if "requestCaptcha" in url:
                return _mock_response(captcha_payload)
            return _mock_response(get_url_payload)

        with patch.object(k2s_client.requests, "post", side_effect=fake_post), \
             patch.object(k2s_client.requests, "get", return_value=_mock_response({})) as mock_get, \
             patch.object(k2s_client, "FuturesSession") as mock_session_cls:
            # No proxy pool -> loop over proxy_pool does nothing, we only care
            # that fetching the captcha image itself passed a timeout before
            # the proxy loop starts.
            mock_session_cls.return_value.post.return_value = None
            with pytest.raises(RuntimeError):
                k2s_client.generate_download_urls(
                    "file123",
                    count=1,
                    proxies=[],
                    captcha_callback=lambda *_: "answer",
                )

        assert mock_get.called
        _, kwargs = mock_get.call_args
        assert kwargs.get("timeout") == k2s_client.DEFAULT_TIMEOUT
