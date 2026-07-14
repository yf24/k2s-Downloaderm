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
