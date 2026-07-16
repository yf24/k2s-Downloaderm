"""Tests for P0-1, P0-2, P0-3/P0-4.

- P0-1: ``parse_size`` must treat KiB/MiB/GiB/TiB as binary (1024-based),
  matching the already-binary KB/MB/GB/TB in this codebase, instead of the
  previous decimal (1000-based) multipliers.
- P0-2: ``_build_ranges`` must produce byte ranges that are contiguous and
  gap/overlap-free for arbitrary (total_size, split_count) pairs, since a
  gap silently drops bytes and an overlap silently duplicates them when the
  parts are concatenated.
- P0-3/P0-4: ``_acquire_proxy_lock`` must never let two callers hold the
  same proxy lock at once, and must not busy-loop forever once
  ``stop_event`` is set, even when many threads contend for a small pool of
  proxies.
"""
from __future__ import annotations

import threading
import time

import pytest

from k2s_downloader.core.downloader import Downloader, parse_size


class TestParseSizeBinaryUnits:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1B", 1),
            ("1KB", 2**10),
            ("1MB", 2**20),
            ("1GB", 2**30),
            ("1TB", 2**40),
            ("1KIB", 2**10),
            ("1MIB", 2**20),
            ("1GIB", 2**30),
            ("1TIB", 2**40),
            ("20M", 20 * 2**20),
            ("5MiB", 5 * 2**20),
            ("5MIB", 5 * 2**20),
            ("100", 100),
        ],
    )
    def test_units_are_binary(self, text, expected):
        assert parse_size(text) == expected

    def test_kb_and_kib_agree(self):
        # KB in this codebase has always meant binary (1024) bytes; KiB is
        # the explicit-binary spelling of the same thing and must parse to
        # an identical value, not a smaller decimal one.
        assert parse_size("7KB") == parse_size("7KiB")

    def test_previously_broken_5mib_no_longer_undershoots_split_floor(self):
        # Before the fix, "5MiB" parsed as 5 * 10**6 = 5,000,000 bytes,
        # which is below the 5 MiB (5,242,880 byte) minimum split size and
        # would incorrectly raise ValueError in Downloader.download().
        assert parse_size("5MiB") >= 5 * 1024 * 1024

    def test_invalid_size_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_size("not-a-size")

    def test_unknown_unit_raises_value_error_not_key_error(self):
        # Before the fix this fell through to `units[unit]` and raised a
        # bare KeyError, which cli.py's `except ValueError` does not catch.
        with pytest.raises(ValueError):
            parse_size("5XB")

    def test_cli_default_split_size_is_parseable(self):
        # cli.py's --split-size default is the literal string "20M". This
        # regression-guards against that default ever becoming unparseable
        # again (previously it raised an uncaught KeyError on every CLI
        # invocation that didn't pass --split-size explicitly).
        from k2s_downloader.cli import build_parser

        default_value = build_parser().get_default("size")
        assert parse_size(default_value) == 20 * 2**20


class TestBuildRangesContiguity:
    @pytest.mark.parametrize(
        "total_size,split_count",
        [
            (1, 1),
            (10, 3),
            (10, 7),
            (1024, 3),
            (1_000_000, 17),
            (7, 7),
            (7, 1),
            (999_999_937, 23),  # prime-ish, forces awkward rounding
        ],
    )
    def test_ranges_are_contiguous_and_cover_total(self, total_size, split_count):
        ranges = Downloader._build_ranges(total_size, split_count)

        assert len(ranges) == split_count

        total_bytes = 0
        expected_start = 0
        for i in range(split_count):
            meta = ranges[str(i)]
            start_str, end_str = str(meta["range"]).split("-")
            start, end = int(start_str), int(end_str)

            assert start == expected_start, f"gap or overlap before range {i}"
            assert end >= start
            assert meta["bytes"] == end - start + 1

            total_bytes += meta["bytes"]
            expected_start = end + 1

        assert total_bytes == total_size
        # The last range must reach exactly the end of the file.
        last_start, last_end = str(ranges[str(split_count - 1)]["range"]).split("-")
        assert int(last_end) == total_size - 1

    def test_first_range_starts_at_zero(self):
        ranges = Downloader._build_ranges(10_000, 4)
        start, _ = str(ranges["0"]["range"]).split("-")
        assert start == "0"


class TestAcquireProxyLockConcurrencySafety:
    def _downloader(self, proxy_count):
        downloader = Downloader()
        downloader.proxies = [None] * proxy_count
        downloader.proxy_locks = [threading.Lock() for _ in range(proxy_count)]
        downloader.working_proxy_indexes = list(range(proxy_count))
        return downloader

    def test_no_two_threads_hold_the_same_proxy_lock_simultaneously(self):
        # Deliberately oversubscribe: more worker threads than proxies, so
        # contention is guaranteed. If the old check-then-act race were
        # still present, this would (non-deterministically) let two threads
        # both believe they own the same lock.
        proxy_count = 3
        worker_count = 12
        downloader = self._downloader(proxy_count)

        violations = []
        held = set()
        held_lock = threading.Lock()
        barrier = threading.Barrier(worker_count)

        def worker():
            barrier.wait()
            idx = downloader._acquire_proxy_lock()
            assert idx is not None
            with held_lock:
                if idx in held:
                    violations.append(idx)
                held.add(idx)
            time.sleep(0.01)
            with held_lock:
                held.discard(idx)
            downloader.proxy_locks[idx].release()

        threads = [threading.Thread(target=worker) for _ in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not any(t.is_alive() for t in threads)
        assert violations == []

    def test_returns_none_promptly_once_cancelled(self):
        # All proxies pre-locked (simulating "everything is busy"); a caller
        # waiting on _acquire_proxy_lock must give up quickly once
        # stop_event is set instead of spinning forever.
        downloader = self._downloader(2)
        for lock in downloader.proxy_locks:
            lock.acquire()

        def cancel_soon():
            time.sleep(0.05)
            downloader.stop_event.set()

        threading.Thread(target=cancel_soon, daemon=True).start()

        start = time.monotonic()
        result = downloader._acquire_proxy_lock()
        elapsed = time.monotonic() - start

        assert result is None
        assert elapsed < 1.0

    def test_working_proxy_indexes_append_is_race_free(self):
        # Many threads racing to record the same "newly known" proxy index
        # must not produce duplicate entries.
        downloader = self._downloader(1)
        downloader.working_proxy_indexes = []
        downloader._notify_proxy_state = lambda: None  # not under test here

        def record():
            with downloader._working_proxy_lock:
                is_new = 0 not in downloader.working_proxy_indexes
                if is_new:
                    downloader.working_proxy_indexes.append(0)

        threads = [threading.Thread(target=record) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert downloader.working_proxy_indexes == [0]
