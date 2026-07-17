"""Tests for P0-2: download_chunk must check the HTTP status code instead of
only relying on a coincidental byte-count mismatch.

A blocked/rate-limited proxy or IP often returns a small error page (403,
429, 5xx) rather than hanging or returning wrong-sized data. Before this
fix, such a response could in rare cases match the expected byte count and
silently corrupt the output; more importantly, there was no visibility into
*why* a chunk failed. These tests exercise the real ``_download_once``
threaded pipeline with the network layer mocked out.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from k2s_downloader.core.downloader import Downloader


def _make_response(status_code: int, body: bytes = b""):
    response = MagicMock()
    response.status_code = status_code
    response.iter_content.return_value = [body] if body else []
    return response


class TestChunkStatusCodeHandling:
    def _downloader(self, tmp_path):
        downloader = Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
            block_size=1024,
        )
        # Normally created by download() before _download_once ever runs;
        # created here too since these tests call _download_once directly.
        downloader.tmp_dir.mkdir(parents=True, exist_ok=True)
        # Normally populated by refresh_proxies(); we only need a single
        # "direct connection" (None) entry for this test.
        downloader.proxies = [None]
        downloader.proxy_locks = [threading.Lock()]
        downloader.working_proxy_indexes = []
        return downloader

    def test_403_response_is_retried_and_eventually_succeeds(self, tmp_path):
        downloader = self._downloader(tmp_path)
        body = b"0123456789"  # 10 bytes, matches the mocked Content-Length below

        head_response = MagicMock()
        head_response.headers = {"Content-Length": str(len(body))}

        blocked_response = _make_response(403)
        ok_response = _make_response(200, body)

        with patch("k2s_downloader.core.downloader.requests.head", return_value=head_response), \
             patch(
                 "k2s_downloader.core.downloader.requests.get",
                 side_effect=[blocked_response, ok_response],
             ) as mock_get:
            result_path = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=len(body),
            )

        assert mock_get.call_count == 2
        assert result_path.read_bytes() == body
        # The blocked attempt must not have been treated as valid data.
        blocked_response.iter_content.assert_not_called()

    def test_non_2xx_status_does_not_write_bad_data_as_success(self, tmp_path):
        downloader = self._downloader(tmp_path)
        body = b"x"

        head_response = MagicMock()
        head_response.headers = {"Content-Length": str(len(body))}

        # Every attempt is blocked; downloader currently has no retry cap
        # (that's P0-3), so bound our patience with a side_effect that runs
        # out and raises StopIteration, which the download loop's broad
        # exception handling inside contextlib.suppress will absorb per
        # attempt -- but repeated attempts would hang the test. We only
        # assert on the first attempt's handling here by limiting threads
        # to 1 and cancelling right after the first failure is observed.
        call_count = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_response(429)
            downloader.stop_event.set()
            return _make_response(429)

        with patch("k2s_downloader.core.downloader.requests.head", return_value=head_response), \
             patch("k2s_downloader.core.downloader.requests.get", side_effect=fake_get):
            with pytest.raises(Exception):
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=len(body),
                )

        assert not (tmp_path / "out.bin").exists()
