"""Tests for R2-P0: concurrency races in the chunk/scheduler hand-off.

- R2-1: `_download_chunk`'s success path used to clear ``inUse`` before
  setting ``downloaded`` (and outside any lock), so the scheduling loop's
  part-file-reuse branch could observe ``inUse=False, downloaded=False``
  with a finished part on disk and count the range a second time.
- R2-2: the size-mismatch path used to release ``url_locks[thread_index]``
  early and then release it *again* in ``finally``; if the scheduler had
  re-assigned the slot in between, the second release freed a lock owned
  by another chunk thread.
- R2-3: `_run_scheduling_loop` used to return without joining in-flight
  chunk threads (and released their locks while they were still running),
  letting an immediate retry race the leftover threads on the same tmp
  part files.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from tqdm import tqdm

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import (
    ChunkDownloadFailed,
    DownloadCancelled,
    Downloader,
    _DownloadContext,
)


def _make_downloader(tmp_path):
    downloader = Downloader(
        tmp_dir=tmp_path / "tmp",
        url_cache_path=tmp_path / "urls.json",
        block_size=1024,
    )
    downloader.proxies = [None]
    downloader.proxy_locks = [threading.Lock()]
    downloader.working_proxy_indexes = []
    return downloader


def _make_context(urls, filename, *, threads=1, split_count=1):
    return _DownloadContext(
        urls=urls,
        filename=filename,
        headers={"User-Agent": "test-agent"},
        threads=threads,
        split_count=split_count,
        progress_bar=tqdm(total=1, disable=True),
    )


def _make_response(status_code, iter_factory):
    response = MagicMock()
    response.status_code = status_code
    response.iter_content.side_effect = iter_factory
    return response


def _new_threads_alive(before):
    return [t for t in threading.enumerate() if t not in before and t.is_alive()]


class TestCompletionPublishOrder:
    """R2-1: chunk completion must set ``downloaded`` before clearing
    ``inUse``, atomically under ``_progress_lock``."""

    def test_downloaded_set_before_in_use_cleared_and_under_lock(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        downloader.tmp_dir.mkdir(parents=True)
        events = []

        # _download_chunk swallows exceptions from its try block by design
        # (a chunk thread must never crash silently), so record what we saw
        # here and assert after the call instead of raising inside the hook.
        class RecordingMeta(dict):
            def __setitem__(self, key, value):
                events.append(
                    (key, value, self.get("downloaded"), downloader._progress_lock.locked())
                )
                super().__setitem__(key, value)

        body = b"x" * 10
        meta = RecordingMeta(inUse=True, downloaded=False, range="0-9", bytes=len(body))
        ctx = _make_context(["https://example.com/f"], "out.bin")
        downloader.url_locks = [threading.Lock()]
        downloader.url_locks[0].acquire()

        response = _make_response(206, lambda block_size: iter([body]))
        with patch("k2s_downloader.core.downloader.requests.get", return_value=response):
            downloader._download_chunk("0", meta, 0, ctx)

        assert meta["downloaded"] is True
        assert downloader._done_count == 1
        in_use_clears = [e for e in events if e[0] == "inUse" and e[1] is False]
        assert len(in_use_clears) == 1
        _, _, downloaded_at_clear, lock_held_at_clear = in_use_clears[0]
        assert downloaded_at_clear is True  # ordering: downloaded published first
        assert lock_held_at_clear  # both writes inside _progress_lock


class TestUrlLockSingleRelease:
    """R2-2: the size-mismatch path must not release the url lock early --
    ``finally`` is the single release point."""

    class InstrumentedLock:
        """A Lock wrapper whose first release immediately hands the lock to a
        simulated competitor (the scheduler re-dispatching this slot)."""

        def __init__(self):
            self._lock = threading.Lock()
            self.release_count = 0
            self.competitor_acquired = False

        def acquire(self, blocking=True):
            return self._lock.acquire(blocking)

        def locked(self):
            return self._lock.locked()

        def release(self):
            self.release_count += 1
            self._lock.release()
            if self.release_count == 1:
                self.competitor_acquired = self._lock.acquire(blocking=False)

    def test_size_mismatch_releases_url_lock_exactly_once(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        downloader.tmp_dir.mkdir(parents=True)

        lock = self.InstrumentedLock()
        downloader.url_locks = [lock]
        lock.acquire()  # as the scheduling loop does before dispatch

        # Body shorter than expected -> size-mismatch path.
        meta = {"inUse": True, "downloaded": False, "range": "0-9", "bytes": 10}
        ctx = _make_context(["https://example.com/f"], "out.bin")
        response = _make_response(206, lambda block_size: iter([b"x" * 5]))
        with patch("k2s_downloader.core.downloader.requests.get", return_value=response):
            downloader._download_chunk("0", meta, 0, ctx)

        assert lock.release_count == 1
        # The competitor that grabbed the slot right after the release must
        # still own it -- a second (unowned) release would have freed it.
        assert lock.competitor_acquired
        assert lock.locked()


class TestSchedulingLoopJoinsChunkThreads:
    """R2-3: `_run_scheduling_loop` must not return while chunk threads it
    spawned are still running."""

    def _head_response(self, total):
        head = MagicMock()
        head.headers = {"Content-Length": str(total)}
        return head

    def test_cancel_waits_for_in_flight_chunk_threads(self, tmp_path):
        downloader = _make_downloader(tmp_path)

        def cancelling_iter(block_size):
            yield b"x"
            downloader.cancel()
            # Keep this chunk thread busy well past the scheduler noticing
            # stop_event (~0.05s): pre-fix, the loop returned while this
            # thread was still inside iter_content.
            time.sleep(0.5)
            yield b"x"

        response = _make_response(206, cancelling_iter)
        threads_before = set(threading.enumerate())
        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=self._head_response(20),
        ), patch("k2s_downloader.core.downloader.requests.get", return_value=response):
            with pytest.raises(DownloadCancelled):
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=10,
                )

        assert _new_threads_alive(threads_before) == []

    def test_permanent_failure_stops_and_joins_in_flight_chunk_threads(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        downloader.proxies = [None, None]
        downloader.proxy_locks = [threading.Lock(), threading.Lock()]

        streaming_started = threading.Event()

        def endless_iter(block_size):
            streaming_started.set()
            while True:
                time.sleep(0.05)
                yield b"x"

        slow_response = _make_response(206, endless_iter)
        blocked_response = _make_response(403, lambda block_size: iter([]))

        get_call_lock = threading.Lock()
        get_calls = []

        def get_side_effect(*args, **kwargs):
            # Exactly one chunk streams forever; every other attempt is
            # blocked and (with MAX_CHUNK_RETRIES=1) permanently fails its
            # range while the slow chunk thread is still in flight.
            with get_call_lock:
                get_calls.append(1)
                return slow_response if len(get_calls) == 1 else blocked_response

        threads_before = set(threading.enumerate())
        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), \
             patch(
                 "k2s_downloader.core.downloader.requests.head",
                 return_value=self._head_response(20),
             ), \
             patch(
                 "k2s_downloader.core.downloader.requests.get",
                 side_effect=get_side_effect,
             ):
            with pytest.raises(ChunkDownloadFailed):
                downloader._download_once(
                    ["https://example.com/a", "https://example.com/b"],
                    str(tmp_path / "out.bin"),
                    threads=2,
                    bytes_per_split=10,
                )

        # The permanent failure must abort the still-streaming chunk thread
        # (via stop_event) and join it before returning.
        assert streaming_started.is_set()
        assert _new_threads_alive(threads_before) == []
        # All locks must be free again for the next download round.
        assert all(not lock.locked() for lock in downloader.url_locks)
        assert all(not lock.locked() for lock in downloader.proxy_locks)
