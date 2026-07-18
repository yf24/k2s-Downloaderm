"""Regression test for the R2-16 revert.

A whole-download auto-retry (catch ChunkDownloadFailed, re-solve the
captcha, re-fetch all download URLs, try again) was added and then reverted
after real-world use showed it made things worse: every automatic retry
forced a fresh captcha prompt *and* re-ran the proxy-cycling search inside
k2s_client.generate_download_urls against R2-10's much larger (mostly-dead)
multi-source pool -- far more disruptive than just letting one chunk keep
trying more proxies within the same session (which is what the much higher
MAX_CHUNK_RETRIES, bumped from 8 to 25 in the same change, is for instead).

This test guards against silently reintroducing that whole-download retry:
a ChunkDownloadFailed must propagate straight out of download() after a
single URL-generation call, not trigger a second one.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from k2s_downloader.core import k2s_client
from k2s_downloader.core.downloader import ChunkDownloadFailed, Downloader


class TestChunkDownloadFailedIsNotAutoRetried:
    def _downloader(self, tmp_path) -> Downloader:
        downloader = Downloader(
            tmp_dir=tmp_path / "tmp",
            url_cache_path=tmp_path / "urls.json",
        )
        downloader.proxies = [None]
        return downloader

    def test_chunk_download_failed_propagates_after_a_single_url_generation_call(self, tmp_path):
        downloader = self._downloader(tmp_path)
        url_gen_calls: list[int] = []

        def fake_generate_urls(*args, **kwargs):
            url_gen_calls.append(1)
            return ["https://dl/1"]

        def fake_download_once(urls, filename, threads, bytes_per_split, file_id=""):
            raise ChunkDownloadFailed("chunk 3 failed after 25 attempts")

        with patch.object(k2s_client, "get_name", return_value="file.bin"), \
             patch.object(k2s_client, "generate_download_urls", side_effect=fake_generate_urls), \
             patch.object(downloader, "_download_once", side_effect=fake_download_once):
            with pytest.raises(ChunkDownloadFailed, match="chunk 3 failed"):
                downloader.download("https://k2s.cc/file/abc123", threads=1, ensure_media_check=False)

        # Exactly one URL-generation (and therefore one captcha) round --
        # no automatic whole-download retry.
        assert len(url_gen_calls) == 1
