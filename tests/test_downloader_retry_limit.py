"""Tests for P0-3: bounded retries with backoff for a single chunk/range.

Previously, if every proxy (and the direct connection) was blocked, the
scheduling loop in ``_download_once`` would retry the same range forever
with no delay and no way to ever surface an error -- this is what made the
CLI/GUI look permanently frozen. These tests cover both the low-level
bookkeeping (``_mark_chunk_failed``) and the end-to-end behaviour once a
chunk exhausts its retry budget.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import ChunkDownloadFailed, Downloader


def _make_response(status_code: int, body: bytes = b""):
    response = MagicMock()
    response.status_code = status_code
    response.iter_content.return_value = [body] if body else []
    return response


class TestMarkChunkFailedBookkeeping:
    def _downloader(self):
        return Downloader()

    def test_attempts_increment_and_backoff_grows(self):
        downloader = self._downloader()
        meta: dict = {}

        with patch.object(downloader_module.time, "time", return_value=1000.0):
            downloader._mark_chunk_failed(meta, "boom 1")
            assert meta["attempts"] == 1
            assert meta["last_error"] == "boom 1"
            assert meta["inUse"] is False
            first_backoff = meta["next_retry_at"] - 1000.0

            downloader._mark_chunk_failed(meta, "boom 2")
            assert meta["attempts"] == 2
            second_backoff = meta["next_retry_at"] - 1000.0

        assert second_backoff > first_backoff  # exponential, not constant

    def test_backoff_is_capped(self):
        downloader = self._downloader()
        # 5 prior failures -> this call becomes attempt #6. With base=1.0 the
        # uncapped exponential backoff would be 2**5 == 32s; it must be
        # clamped to CHUNK_RETRY_BACKOFF_CAP (30s) instead.
        meta: dict = {"attempts": 5}

        with patch.object(downloader_module.time, "time", return_value=1000.0):
            downloader._mark_chunk_failed(meta, "still failing")

        assert meta["attempts"] == 6
        assert "failed" not in meta  # below MAX_CHUNK_RETRIES, still retryable
        backoff = meta["next_retry_at"] - 1000.0
        assert backoff == pytest.approx(downloader_module.CHUNK_RETRY_BACKOFF_CAP)

    def test_marked_failed_after_max_retries(self):
        downloader = self._downloader()
        meta: dict = {}

        for _ in range(downloader_module.MAX_CHUNK_RETRIES - 1):
            downloader._mark_chunk_failed(meta, "fail")
            assert "failed" not in meta

        downloader._mark_chunk_failed(meta, "final fail")
        assert meta["failed"] is True
        assert meta["attempts"] == downloader_module.MAX_CHUNK_RETRIES


class TestEndToEndRetryExhaustion:
    def _downloader(self, tmp_path):
        downloader = Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
            block_size=1024,
        )
        downloader.proxies = [None]
        downloader.proxy_locks = [threading.Lock()]
        downloader.working_proxy_indexes = []
        return downloader

    def test_raises_chunk_download_failed_when_every_attempt_is_blocked(self, tmp_path):
        downloader = self._downloader(tmp_path)
        body = b"x"
        head_response = MagicMock()
        head_response.headers = {"Content-Length": str(len(body))}

        # Keep the test fast: shrink the retry budget and backoff instead of
        # waiting through the real (up to 30s-capped) production backoff.
        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 3), \
             patch.object(downloader_module, "CHUNK_RETRY_BACKOFF_BASE", 0.01), \
             patch.object(downloader_module, "CHUNK_RETRY_BACKOFF_CAP", 0.02), \
             patch("k2s_downloader.core.downloader.requests.head", return_value=head_response), \
             patch(
                 "k2s_downloader.core.downloader.requests.get",
                 return_value=_make_response(403),
             ) as mock_get:
            start = time.monotonic()
            with pytest.raises(ChunkDownloadFailed, match="HTTP 403"):
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=len(body),
                )
            elapsed = time.monotonic() - start

        assert mock_get.call_count == 3
        assert not (tmp_path / "out.bin").exists()
        # Sanity check that this genuinely exercised the bounded-retry path
        # and not a fast-fail/no-retry shortcut.
        assert elapsed < 5.0

    def test_status_callback_reports_progress_before_giving_up(self, tmp_path):
        downloader = self._downloader(tmp_path)
        messages: list[str] = []
        downloader.status_callback = messages.append

        body = b"x"
        head_response = MagicMock()
        head_response.headers = {"Content-Length": str(len(body))}

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 2), \
             patch.object(downloader_module, "CHUNK_RETRY_BACKOFF_BASE", 0.01), \
             patch.object(downloader_module, "CHUNK_RETRY_BACKOFF_CAP", 0.02), \
             patch("k2s_downloader.core.downloader.requests.head", return_value=head_response), \
             patch("k2s_downloader.core.downloader.requests.get", return_value=_make_response(429)):
            with pytest.raises(ChunkDownloadFailed):
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=len(body),
                )

        assert any("failed" in m.lower() for m in messages)
