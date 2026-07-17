"""Tests for R2-10 item 4: evict repeatedly-failing proxies.

Previously a proxy index was only ever added to ``working_proxy_indexes``
(on a successful chunk), never removed -- a proxy that degraded partway
through a download (or was flaky from the start) kept being preferentially
reselected by ``_acquire_proxy_lock`` for the rest of the run. Consecutive
chunk-download failures through the same proxy index are now tracked and,
past ``PROXY_FAILURE_EVICTION_THRESHOLD``, the proxy is dropped from
``working_proxy_indexes`` -- though it remains reachable via the random
fallback tier, and a later success clears its count and re-adds it, so a
proxy that recovers becomes eligible again rather than being permanently
blacklisted.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import Downloader


def _make_downloader(proxy_count: int) -> Downloader:
    downloader = Downloader()
    downloader.proxies = [None] + [f"1.2.3.{i}:8080" for i in range(1, proxy_count)]
    downloader.proxy_locks = [threading.Lock() for _ in range(proxy_count)]
    downloader.working_proxy_indexes = list(range(1, proxy_count))
    return downloader


class TestNoteProxyFailure:
    def test_direct_connection_index_zero_is_never_tracked_or_evicted(self):
        downloader = _make_downloader(2)
        downloader.working_proxy_indexes = [0]  # shouldn't normally happen, but prove it's inert either way

        for _ in range(downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD + 5):
            downloader._note_proxy_failure(0)

        assert 0 not in downloader._proxy_consecutive_failures
        assert downloader.working_proxy_indexes == [0]

    def test_proxy_stays_in_working_list_below_the_eviction_threshold(self):
        downloader = _make_downloader(2)
        for _ in range(downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD - 1):
            downloader._note_proxy_failure(1)

        assert 1 in downloader.working_proxy_indexes

    def test_proxy_is_evicted_once_the_eviction_threshold_is_reached(self):
        downloader = _make_downloader(2)
        for _ in range(downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD):
            downloader._note_proxy_failure(1)

        assert 1 not in downloader.working_proxy_indexes

    def test_eviction_is_logged(self):
        messages: list[str] = []
        downloader = _make_downloader(2)
        downloader.status_callback = messages.append
        for _ in range(downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD):
            downloader._note_proxy_failure(1)

        assert any("deprioritizing" in msg for msg in messages)

    def test_evicted_proxy_can_be_reselected_via_the_random_fallback_tier(self):
        # Eviction only removes the proxy from the "known good" tier of
        # _acquire_proxy_lock -- it's still a valid index into self.proxies
        # and can still be picked by the random-fallback tier.
        downloader = _make_downloader(2)
        for _ in range(downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD):
            downloader._note_proxy_failure(1)
        downloader.proxy_locks[0].acquire()  # force past the direct-connection tier

        with patch.object(downloader_module.random, "randint", return_value=1):
            idx = downloader._acquire_proxy_lock()

        assert idx == 1


class TestMarkChunkFailedFeedsEviction:
    def test_mark_chunk_failed_with_proxy_idx_counts_toward_eviction(self):
        downloader = _make_downloader(2)
        range_meta: dict[str, object] = {}
        for _ in range(downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD):
            downloader._mark_chunk_failed(range_meta, "boom", proxy_idx=1)

        assert 1 not in downloader.working_proxy_indexes

    def test_mark_chunk_failed_without_proxy_idx_does_not_touch_eviction_state(self):
        downloader = _make_downloader(2)
        range_meta: dict[str, object] = {}
        for _ in range(downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD + 5):
            downloader._mark_chunk_failed(range_meta, "boom")

        assert downloader._proxy_consecutive_failures == {}
        assert 1 in downloader.working_proxy_indexes


class TestRefreshProxiesResetsFailureState:
    def test_refresh_proxies_clears_consecutive_failure_counts(self):
        downloader = _make_downloader(2)
        downloader._proxy_consecutive_failures[1] = 2

        with patch.object(downloader_module, "get_working_proxies", return_value=[None]):
            downloader.refresh_proxies()

        assert downloader._proxy_consecutive_failures == {}
        assert downloader.working_proxy_indexes == []


class TestSuccessfulChunkResetsFailureCount:
    def _head(self, total: int) -> MagicMock:
        head = MagicMock()
        head.headers = {"Content-Length": str(total)}
        return head

    def _response(self, status_code: int, body: bytes) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.iter_content.side_effect = lambda block_size: iter([body])
        return response

    def test_success_via_a_proxy_clears_its_prior_failure_streak(self, tmp_path):
        downloader = Downloader(tmp_dir=tmp_path / "tmp", url_cache_path=tmp_path / "urls.json", block_size=1)
        downloader.tmp_dir.mkdir(parents=True, exist_ok=True)
        downloader.proxies = [None, "1.2.3.4:8080"]
        downloader.proxy_locks = [threading.Lock(), threading.Lock()]
        downloader.proxy_locks[0].acquire()  # force the chunk onto proxy index 1
        downloader.working_proxy_indexes = [1]
        downloader._proxy_consecutive_failures[1] = downloader_module.PROXY_FAILURE_EVICTION_THRESHOLD - 1

        body = b"hello"
        with patch("k2s_downloader.core.downloader.requests.head", return_value=self._head(len(body))), \
             patch("k2s_downloader.core.downloader.requests.get", return_value=self._response(206, body)):
            downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=len(body),
            )

        assert 1 not in downloader._proxy_consecutive_failures
        assert 1 in downloader.working_proxy_indexes
