"""Tests for the whole-download auto-retry added after a real user session
hit a permanently-failed chunk and had to manually restart the app to try
again.

``download()`` already retried a single chunk up to MAX_CHUNK_RETRIES times
before giving up with ``ChunkDownloadFailed`` -- but once that happened, the
whole download aborted with no further automatic action, even though R2-13's
resume manifest meant a fresh attempt would only need to redo the actually-
stuck chunk(s), not the whole file. ``download()`` now catches
``ChunkDownloadFailed`` and retries the whole call (fresh captcha + URLs,
same resolved filename/tmp_dir so resume kicks in) up to
``MAX_DOWNLOAD_RETRIES`` times before finally letting the failure surface.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core import k2s_client
from k2s_downloader.core.downloader import ChunkDownloadFailed, DownloadCancelled, Downloader


class TestWholeDownloadRetry:
    def _downloader(self, tmp_path) -> Downloader:
        downloader = Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
        )
        downloader.proxies = [None]
        return downloader

    def test_a_permanently_failed_chunk_triggers_an_automatic_whole_download_retry(self, tmp_path):
        downloader = self._downloader(tmp_path)
        attempts: list[int] = []

        def fake_download_once(urls, filename, threads, bytes_per_split, file_id=""):
            attempts.append(1)
            if len(attempts) == 1:
                raise ChunkDownloadFailed("chunk 3 failed after 8 attempts")
            return tmp_path / filename

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(k2s_client, "generate_download_urls", return_value=["https://dl/1"]), \
             patch.object(downloader, "_download_once", side_effect=fake_download_once):
            result = downloader.download(
                "https://k2s.cc/file/abc123", threads=1, ensure_media_check=False
            )

        assert len(attempts) == 2  # failed once, succeeded on the automatic retry
        assert result == tmp_path / "file.bin"

    def test_retry_solves_a_fresh_captcha_each_time_not_just_the_first(self, tmp_path):
        downloader = self._downloader(tmp_path)
        url_gen_calls: list[int] = []

        def fake_generate_urls(*args, **kwargs):
            url_gen_calls.append(1)
            return ["https://dl/1"]

        def fake_download_once(urls, filename, threads, bytes_per_split, file_id=""):
            if len(url_gen_calls) < 2:
                raise ChunkDownloadFailed("stuck")
            return tmp_path / filename

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(k2s_client, "generate_download_urls", side_effect=fake_generate_urls), \
             patch.object(downloader, "_download_once", side_effect=fake_download_once):
            downloader.download("https://k2s.cc/file/abc123", threads=1, ensure_media_check=False)

        assert len(url_gen_calls) == 2

    def test_exhausting_all_retries_reraises_chunk_download_failed(self, tmp_path):
        downloader = self._downloader(tmp_path)
        attempts: list[int] = []

        def fake_download_once(urls, filename, threads, bytes_per_split, file_id=""):
            attempts.append(1)
            raise ChunkDownloadFailed("permanently stuck")

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(k2s_client, "generate_download_urls", return_value=["https://dl/1"]), \
             patch.object(downloader, "_download_once", side_effect=fake_download_once):
            with pytest.raises(ChunkDownloadFailed, match="permanently stuck"):
                downloader.download("https://k2s.cc/file/abc123", threads=1, ensure_media_check=False)

        assert len(attempts) == downloader_module.MAX_DOWNLOAD_RETRIES

    def test_cancellation_is_not_retried(self, tmp_path):
        downloader = self._downloader(tmp_path)
        attempts: list[int] = []

        def fake_download_once(urls, filename, threads, bytes_per_split, file_id=""):
            attempts.append(1)
            downloader.stop_event.set()
            return tmp_path / filename

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(k2s_client, "generate_download_urls", return_value=["https://dl/1"]), \
             patch.object(downloader, "_download_once", side_effect=fake_download_once):
            with pytest.raises(DownloadCancelled):
                downloader.download("https://k2s.cc/file/abc123", threads=1, ensure_media_check=False)

        assert len(attempts) == 1  # cancellation must never trigger a retry

    def test_non_chunk_failure_exceptions_are_not_retried(self, tmp_path):
        downloader = self._downloader(tmp_path)
        attempts: list[int] = []

        def fake_download_once(urls, filename, threads, bytes_per_split, file_id=""):
            attempts.append(1)
            raise RuntimeError("every proxy exhausted")

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(k2s_client, "generate_download_urls", return_value=["https://dl/1"]), \
             patch.object(downloader, "_download_once", side_effect=fake_download_once):
            with pytest.raises(RuntimeError, match="every proxy exhausted"):
                downloader.download("https://k2s.cc/file/abc123", threads=1, ensure_media_check=False)

        assert len(attempts) == 1
