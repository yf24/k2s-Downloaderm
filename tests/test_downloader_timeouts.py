"""Tests for P0-1: the HEAD request used to discover file size must have a
timeout, and a failure there should raise a clear error instead of hanging.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from k2s_downloader.core.downloader import Downloader, HEAD_REQUEST_TIMEOUT


class TestSizeDiscoveryHeadRequest:
    def _downloader(self, tmp_path):
        return Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
        )

    def test_head_request_uses_timeout(self, tmp_path):
        downloader = self._downloader(tmp_path)
        mock_head_response = MagicMock()
        mock_head_response.headers = {"Content-Length": "100"}

        with patch("k2s_downloader.core.downloader.requests.head", return_value=mock_head_response) as mock_head:
            # threads=1 keeps this simple; we only care about the HEAD call
            # itself, so stub out the rest of the range-download machinery.
            downloader.url_locks = []
            with patch.object(downloader, "_build_ranges", return_value={}):
                try:
                    downloader._download_once(["https://example.com/f"], str(tmp_path / "out.bin"), 1, 10)
                except Exception:
                    pass

        assert mock_head.called
        _, kwargs = mock_head.call_args
        assert kwargs.get("timeout") == HEAD_REQUEST_TIMEOUT

    def test_head_request_timeout_raises_clear_error(self, tmp_path):
        downloader = self._downloader(tmp_path)

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            side_effect=requests.exceptions.Timeout("boom"),
        ):
            with pytest.raises(RuntimeError, match="possibly blocked"):
                downloader._download_once(["https://example.com/f"], str(tmp_path / "out.bin"), 1, 10)


class TestSizeDiscoveryRejectsNonSuccessStatus:
    """R2-6: a HEAD response with a non-2xx status must not have its
    Content-Length (the size of the error page, not the real file) used to
    build byte ranges -- that silently produces ranges for the wrong total
    size, so every chunk then fails with a "size mismatch" that never
    points at the real cause (an expired/blocked download URL).
    """

    def _downloader(self, tmp_path):
        return Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
        )

    def _make_head_response(self, status_code: int, content_length: str = "9999"):
        response = MagicMock()
        response.status_code = status_code
        response.ok = status_code < 400
        response.headers = {"Content-Length": content_length}
        return response

    def test_403_head_response_raises_before_reading_content_length(self, tmp_path):
        downloader = self._downloader(tmp_path)

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=self._make_head_response(403),
        ):
            with pytest.raises(RuntimeError, match="HTTP 403"):
                downloader._download_once(["https://example.com/f"], str(tmp_path / "out.bin"), 1, 10)

    def test_error_message_hints_at_expired_or_blocked_url(self, tmp_path):
        downloader = self._downloader(tmp_path)

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=self._make_head_response(429),
        ):
            with pytest.raises(RuntimeError, match="expired or be blocked"):
                downloader._download_once(["https://example.com/f"], str(tmp_path / "out.bin"), 1, 10)

    def test_5xx_head_response_also_rejected(self, tmp_path):
        downloader = self._downloader(tmp_path)

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=self._make_head_response(503),
        ):
            with pytest.raises(RuntimeError, match="HTTP 503"):
                downloader._download_once(["https://example.com/f"], str(tmp_path / "out.bin"), 1, 10)

    def test_successful_head_response_still_works(self, tmp_path):
        downloader = self._downloader(tmp_path)

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=self._make_head_response(200, content_length="10"),
        ):
            downloader.url_locks = []
            with patch.object(downloader, "_build_ranges", return_value={}) as mock_build_ranges:
                downloader._download_once(["https://example.com/f"], str(tmp_path / "out.bin"), 1, 10)

        mock_build_ranges.assert_called_once_with(10, 1)
