"""Tests for the "blocked IP/proxy" UX fixes in ``generate_download_urls``.

User-reported symptoms: when the IP/proxies are blocked, entering the captcha
appeared to do nothing — the URL-generation phase looped forever with no
error message and no way to cancel. These tests pin down:

- bounded captcha retries with a clear error,
- ``K2SFileNotFound`` raised instead of ``sys.exit`` (which killed the GUI),
- bounded getUrl batch rounds instead of an infinite ``while`` loop,
- partial URL batches being returned instead of spinning forever,
- cooperative cancellation via ``stop_event`` during URL generation.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from k2s_downloader.core import k2s_client
from k2s_downloader.core.downloader import DownloadCancelled, Downloader
from k2s_downloader.core.k2s_client import K2SFileNotFound, OperationCancelled


CAPTCHA_PAYLOAD = {"challenge": "c", "captcha_url": "https://k2s.cc/captcha.png"}


def _mock_response(json_data):
    mock = MagicMock()
    mock.json.return_value = json_data
    mock.content = b"fake-image-bytes"
    return mock


def _post_router(get_url_payload):
    """Route requestCaptcha vs getUrl posts to their canned payloads."""

    def fake_post(url, **kwargs):
        if "requestCaptcha" in url:
            return _mock_response(CAPTCHA_PAYLOAD)
        return _mock_response(get_url_payload)

    return fake_post


class TestCaptchaRetryBound:
    def test_gives_up_after_max_captcha_attempts(self):
        callback_calls = []

        def captcha_callback(image, challenge, url):
            callback_calls.append(challenge)
            return "wrong-answer"

        invalid = {"status": "error", "message": "Invalid captcha code"}
        with patch.object(k2s_client.requests, "post", side_effect=_post_router(invalid)), \
             patch.object(k2s_client.requests, "get", return_value=_mock_response({})):
            with pytest.raises(RuntimeError, match="Captcha rejected"):
                k2s_client.generate_download_urls(
                    "file123",
                    count=1,
                    proxies=[None],
                    captcha_callback=captcha_callback,
                )

        assert len(callback_calls) == k2s_client.MAX_CAPTCHA_ATTEMPTS


class TestFileNotFound:
    def test_raises_exception_instead_of_sys_exit(self):
        not_found = {"status": "error", "message": "File not found"}
        with patch.object(k2s_client.requests, "post", side_effect=_post_router(not_found)), \
             patch.object(k2s_client.requests, "get", return_value=_mock_response({})):
            # Must be a catchable exception, NOT SystemExit: the old sys.exit
            # tore down the whole GUI process from a worker thread.
            with pytest.raises(K2SFileNotFound):
                k2s_client.generate_download_urls(
                    "file123",
                    count=1,
                    proxies=[None],
                    captcha_callback=lambda *_: "answer",
                )


class TestUrlBatchRoundsBounded:
    def _run(self, count, future_results):
        """Drive generate_download_urls with a mocked FuturesSession.

        ``future_results`` is a list of per-future outcomes: a string is a
        URL to return, an Exception instance is raised from ``result()``.
        """
        key_ok = {"free_download_key": "key123"}
        made_futures = []

        def make_future(*args, **kwargs):
            outcome = future_results[len(made_futures)]
            future = MagicMock()
            if isinstance(outcome, Exception):
                future.result.side_effect = outcome
            else:
                future.result.return_value = _mock_response({"url": outcome})
            made_futures.append(future)
            return future

        session = MagicMock()
        session.post.side_effect = make_future

        error = None
        urls = None
        with patch.object(k2s_client.requests, "post", side_effect=_post_router(key_ok)), \
             patch.object(k2s_client.requests, "get", return_value=_mock_response({})), \
             patch.object(k2s_client, "FuturesSession", return_value=session), \
             patch.object(k2s_client, "as_completed", side_effect=lambda futures: list(futures)):
            try:
                urls = k2s_client.generate_download_urls(
                    "file123",
                    count=count,
                    proxies=[None],
                    captcha_callback=lambda *_: "answer",
                    status_callback=lambda _msg: None,
                )
            except RuntimeError as exc:
                error = exc
        return urls, session.post.call_count, error

    def test_raises_clear_error_when_every_geturl_fails(self):
        count = 4
        budget = count * k2s_client.MAX_URL_BATCH_ROUNDS
        start = time.monotonic()
        urls, _, error = self._run(count, [ConnectionError("blocked")] * budget)
        elapsed = time.monotonic() - start

        assert urls is None
        assert error is not None and "blocked or rate-limited" in str(error)
        assert elapsed < 5.0  # bounded, not the old infinite spin

    def test_stops_after_max_rounds_without_progress(self):
        count = 4
        budget = count * k2s_client.MAX_URL_BATCH_ROUNDS
        _, post_calls, error = self._run(count, [ConnectionError("blocked")] * (budget + 10))

        assert error is not None
        assert post_calls == budget  # exactly MAX_URL_BATCH_ROUNDS rounds, then give up

    def test_returns_partial_urls_instead_of_spinning(self):
        # Round 1: 2 of 4 succeed; every later round fails -> should return
        # the 2 URLs it has rather than looping forever chasing all 4.
        outcomes = ["https://dl/1", "https://dl/2", ConnectionError("x"), ConnectionError("x")]
        outcomes += [ConnectionError("x")] * (2 * k2s_client.MAX_URL_BATCH_ROUNDS)
        urls, _, error = self._run(4, outcomes)

        assert error is None
        assert urls == ["https://dl/1", "https://dl/2"]


class TestStopEventCancellation:
    def test_pre_set_stop_event_aborts_before_any_network_call(self):
        stop = threading.Event()
        stop.set()
        captcha_calls = []

        with patch.object(k2s_client.requests, "post") as mock_post, \
             patch.object(k2s_client.requests, "get") as mock_get:
            with pytest.raises(OperationCancelled):
                k2s_client.generate_download_urls(
                    "file123",
                    count=1,
                    proxies=[None],
                    captcha_callback=lambda *a: captcha_calls.append(a) or "x",
                    stop_event=stop,
                )

        assert not mock_post.called
        assert not mock_get.called
        assert not captcha_calls

    def test_cancel_during_proxy_loop_aborts(self):
        stop = threading.Event()

        def captcha_callback(image, challenge, url):
            # Simulate the user pressing Cancel while the captcha prompt /
            # proxy loop is active.
            stop.set()
            return "answer"

        with patch.object(k2s_client.requests, "post", side_effect=_post_router({})), \
             patch.object(k2s_client.requests, "get", return_value=_mock_response({})):
            with pytest.raises(OperationCancelled):
                k2s_client.generate_download_urls(
                    "file123",
                    count=1,
                    proxies=[None],
                    captcha_callback=captcha_callback,
                    stop_event=stop,
                )


class TestDownloaderIntegration:
    def _downloader(self, tmp_path):
        return Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
        )

    def test_operation_cancelled_translates_to_download_cancelled(self, tmp_path):
        downloader = self._downloader(tmp_path)
        downloader.proxies = [None]

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(
                 k2s_client,
                 "generate_download_urls",
                 side_effect=OperationCancelled("cancelled"),
             ):
            with pytest.raises(DownloadCancelled):
                downloader.download("https://k2s.cc/file/abc123", threads=4)

    def test_threads_clamped_to_available_urls(self, tmp_path):
        downloader = self._downloader(tmp_path)
        downloader.proxies = [None]
        seen = {}

        def fake_download_once(urls, filename, threads, bytes_per_split, file_id=""):
            seen["threads"] = threads
            seen["urls"] = list(urls)
            return tmp_path / filename

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(
                 k2s_client,
                 "generate_download_urls",
                 return_value=["https://dl/1", "https://dl/2"],
             ), \
             patch.object(downloader, "_download_once", side_effect=fake_download_once):
            downloader.download(
                "https://k2s.cc/file/abc123",
                threads=8,
                ensure_media_check=False,
            )

        assert seen["threads"] == 2
        assert len(seen["urls"]) == 2
