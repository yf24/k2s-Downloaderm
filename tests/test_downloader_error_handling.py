"""Tests for P2-1: download_chunk must not silently swallow exceptions.

Previously the entire GET + streaming block was wrapped in
``contextlib.suppress(Exception)``, so any network failure (connection
error, read timeout, a chunked-encoding error mid-stream, ...) was silently
discarded and only surfaced later as a coincidental "size mismatch" with no
indication of the real cause. These tests verify that request-level
exceptions -- and any other unexpected exception past the request stage --
are recorded via ``_mark_chunk_failed`` with a diagnosable reason instead,
and that the chunk always comes back out of "inUse" instead of hanging the
scheduling loop.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import ChunkDownloadFailed, Downloader, _truncate_error_message


class TestTruncateErrorMessage:
    def test_long_query_string_is_truncated(self):
        url_tail = "?temp_url_sig=" + "a" * 200 + "&client_ip=1.2.3.4"
        message = f"HTTPSConnectionPool(host='example.com', port=443): Max retries exceeded with url: /f{url_tail} (Caused by ...)"
        result = _truncate_error_message(message)
        assert "temp_url_sig" not in result
        assert "?<truncated>" in result
        # The parts before/after the query string (proxy/exception context) survive.
        assert result.startswith("HTTPSConnectionPool(host='example.com', port=443)")
        assert result.endswith("(Caused by ...)")

    def test_short_query_string_is_left_alone(self):
        message = "some error ?a=1 happened"
        assert _truncate_error_message(message) == message

    def test_message_without_a_query_string_is_unchanged(self):
        message = "Connection to 1.2.3.4 timed out. (connect timeout=20)"
        assert _truncate_error_message(message) == message

    def test_mark_chunk_failed_truncates_reason_before_logging_and_storing(self, tmp_path):
        log: list[str] = []
        downloader = Downloader(tmp_dir=tmp_path / "tmp", status_callback=log.append)
        range_meta: dict[str, object] = {}
        long_reason = "request error via proxy 1.2.3.4:8080: url: /f?sig=" + "b" * 100

        downloader._mark_chunk_failed(range_meta, long_reason)

        assert "b" * 100 not in log[-1]
        assert "b" * 100 not in range_meta["last_error"]


class TestChunkExceptionHandling:
    def _downloader(self, tmp_path):
        downloader = Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
            block_size=1024,
        )
        # Normally created by download() before _download_once ever runs;
        # created here too since these tests call _download_once directly.
        downloader.tmp_dir.mkdir(parents=True, exist_ok=True)
        downloader.proxies = [None]
        downloader.proxy_locks = [threading.Lock()]
        downloader.working_proxy_indexes = []
        return downloader

    def test_connection_error_is_recorded_with_a_diagnosable_reason(self, tmp_path):
        downloader = self._downloader(tmp_path)
        body = b"x"
        head_response = MagicMock()
        head_response.headers = {"Content-Length": str(len(body))}

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), \
             patch("k2s_downloader.core.downloader.requests.head", return_value=head_response), \
             patch(
                 "k2s_downloader.core.downloader.requests.get",
                 side_effect=requests.exceptions.ConnectionError("refused"),
             ) as mock_get:
            with pytest.raises(ChunkDownloadFailed, match="request error") as exc_info:
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=len(body),
                )

        # The old behaviour swallowed this and only ever reported a
        # coincidental byte-count mismatch, never the real cause.
        assert "size mismatch" not in str(exc_info.value)
        assert "refused" in str(exc_info.value)
        assert not (tmp_path / "out.bin").exists()

        # Regression guard for P2-2: the request timeout must come from the
        # named constant, not a re-inlined magic number.
        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == downloader_module.CHUNK_REQUEST_TIMEOUT

    def test_unexpected_non_request_exception_is_recorded_not_left_hanging(self, tmp_path):
        downloader = self._downloader(tmp_path)
        body = b"x"
        head_response = MagicMock()
        head_response.headers = {"Content-Length": str(len(body))}

        bad_response = MagicMock()
        bad_response.status_code = 200
        # Deliberately not a requests.exceptions.RequestException, to prove
        # the outer safety net (not just the narrow request-error handler)
        # catches it.
        bad_response.iter_content.side_effect = ValueError("boom")

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), \
             patch("k2s_downloader.core.downloader.requests.head", return_value=head_response), \
             patch("k2s_downloader.core.downloader.requests.get", return_value=bad_response):
            with pytest.raises(ChunkDownloadFailed, match="unexpected error") as exc_info:
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=len(body),
                )

        assert "boom" in str(exc_info.value)
        assert not (tmp_path / "out.bin").exists()
