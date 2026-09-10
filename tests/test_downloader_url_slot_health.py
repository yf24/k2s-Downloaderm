"""Tests for R3-6: per-download-URL-slot failure tracking and cooldown.

Keep2Share issues one download URL per slot, each bound to the first IP that
uses it, so a single slot can die while every other one keeps working. The
scheduler used to scan slots from index 0 and take the first unlocked one --
and a slot that fails in a second is unlocked again long before a healthy
slot that is minutes into a segment, so a broken URL was effectively
*preferred* and soaked up the retries of range after range. Slots now carry a
failure streak, get benched for a cooldown once it crosses the threshold, and
are scanned from a rotating cursor.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import Downloader

GET_TARGET = "k2s_downloader.core.downloader.requests.get"
HEAD_TARGET = "k2s_downloader.core.downloader.requests.head"


def _make_downloader(tmp_path, *, slots: int = 3):
    downloader = Downloader(
        tmp_dir=tmp_path / "tmp",
        url_cache_path=tmp_path / "urls.json",
        block_size=1024,
    )
    downloader.tmp_dir.mkdir(parents=True, exist_ok=True)
    downloader.proxies = [None]
    downloader.proxy_locks = [threading.Lock()]
    downloader.working_proxy_indexes = []
    downloader.url_locks = [threading.Lock() for _ in range(slots)]
    return downloader


def _head(total: int) -> MagicMock:
    head = MagicMock()
    head.headers = {"Content-Length": str(total)}
    return head


def _response(status_code: int, body: bytes = b"") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    response.iter_content.side_effect = lambda block_size: iter([body] if body else [])
    return response


class TestSlotCooldown:
    def test_slot_is_benched_after_consecutive_failures(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        messages: list[str] = []
        downloader.status_callback = messages.append

        for _ in range(downloader_module.URL_SLOT_FAILURE_THRESHOLD):
            downloader._note_url_slot_failure(0)

        assert downloader._acquire_url_slot() != 0
        assert any("resting it" in message for message in messages)

    def test_success_clears_the_streak(self, tmp_path):
        downloader = _make_downloader(tmp_path)

        for _ in range(downloader_module.URL_SLOT_FAILURE_THRESHOLD - 1):
            downloader._note_url_slot_failure(0)
        downloader._note_url_slot_success(0)
        downloader._note_url_slot_failure(0)

        assert downloader._url_slot_cooldown_until == {}

    def test_benched_slot_becomes_eligible_again_once_the_cooldown_expires(self, tmp_path):
        downloader = _make_downloader(tmp_path, slots=1)

        with patch.object(downloader_module.time, "time", return_value=1000.0):
            for _ in range(downloader_module.URL_SLOT_FAILURE_THRESHOLD):
                downloader._note_url_slot_failure(0)
            assert downloader._acquire_url_slot() is None

        later = 1000.0 + downloader_module.URL_SLOT_COOLDOWN_SECONDS + 1
        with patch.object(downloader_module.time, "time", return_value=later):
            assert downloader._acquire_url_slot() == 0

    def test_scan_rotates_instead_of_always_starting_at_slot_zero(self, tmp_path):
        downloader = _make_downloader(tmp_path)

        handed_out = []
        for _ in range(3):
            thread_index = downloader._acquire_url_slot()
            handed_out.append(thread_index)
            downloader.url_locks[thread_index].release()

        assert handed_out == [0, 1, 2]


class TestBrokenSlotDoesNotSoakUpTheDownload:
    def test_failing_url_is_benched_and_the_healthy_one_finishes_the_download(self, tmp_path):
        downloader = _make_downloader(tmp_path, slots=2)
        total_body = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"  # 32 bytes -> 4 ranges of 8
        calls: dict[str, int] = {"https://bad.example/f": 0, "https://good.example/f": 0}

        def get_side_effect(url, *args, **kwargs):
            calls[url] += 1
            if url == "https://bad.example/f":
                return _response(403)
            first, last = (
                int(part) for part in kwargs["headers"]["Range"].split("=")[1].split("-")
            )
            return _response(206, total_body[first : last + 1])

        with patch.object(downloader_module, "CHUNK_RETRY_BACKOFF_BASE", 0.01), patch.object(
            downloader_module, "CHUNK_RETRY_BACKOFF_CAP", 0.02
        ), patch(HEAD_TARGET, return_value=_head(len(total_body))), patch(
            GET_TARGET, side_effect=get_side_effect
        ):
            result = downloader._download_once(
                ["https://bad.example/f", "https://good.example/f"],
                str(tmp_path / "out.bin"),
                threads=2,
                bytes_per_split=8,
                file_id="file-abc",
            )

        assert result.read_bytes() == total_body
        # The broken URL is tried until it is benched, and then left alone --
        # without the cooldown it would keep being the first free slot and
        # would be handed every retry for the rest of the download.
        assert calls["https://bad.example/f"] <= downloader_module.URL_SLOT_FAILURE_THRESHOLD
        assert calls["https://good.example/f"] >= 4


class TestRetryBudgetRestartsOnProgress:
    """R3-7: the budget counts consecutive failures *without* progress."""

    def test_budget_restarts_when_the_download_progressed_in_between(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        meta: dict = {}

        downloader._mark_chunk_failed(meta, "boom")
        assert meta["attempts"] == 1

        downloader._progress_token += 1  # some other range delivered bytes
        downloader._mark_chunk_failed(meta, "boom again")

        assert meta["attempts"] == 1

    def test_budget_keeps_counting_while_nothing_progresses(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        meta: dict = {}

        downloader._mark_chunk_failed(meta, "boom")
        downloader._mark_chunk_failed(meta, "boom again")

        assert meta["attempts"] == 2

    def test_download_survives_more_failures_than_the_budget_if_it_keeps_progressing(
        self, tmp_path
    ):
        """The regression this round exists for.

        One range fails twice, but the download completes other ranges in
        between. With ``MAX_CHUNK_RETRIES`` at 2 the old cumulative counter
        aborted the whole download on that second failure; the budget now
        restarts, so the run finishes.
        """
        downloader = _make_downloader(tmp_path, slots=1)
        total_body = b"AAAAABBBBBCCCCC"  # 15 bytes -> three 5-byte ranges
        attempts_at_first_range = {"count": 0}

        def get_side_effect(*args, **kwargs):
            first, last = (
                int(part) for part in kwargs["headers"]["Range"].split("=")[1].split("-")
            )
            if first == 0:
                attempts_at_first_range["count"] += 1
                if attempts_at_first_range["count"] <= 2:
                    return _response(503)
            return _response(206, total_body[first : last + 1])

        # Backoff long enough that the other ranges are dispatched between the
        # flaky range's attempts (each mocked request finishes instantly, and
        # the scheduling loop polls every 50ms).
        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 2), patch.object(
            downloader_module, "CHUNK_RETRY_BACKOFF_BASE", 0.15
        ), patch.object(downloader_module, "CHUNK_RETRY_BACKOFF_CAP", 0.15), patch(
            HEAD_TARGET, return_value=_head(len(total_body))
        ), patch(GET_TARGET, side_effect=get_side_effect):
            result = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=5,
                file_id="file-abc",
            )

        assert result.read_bytes() == total_body
        assert attempts_at_first_range["count"] == 3


@pytest.mark.parametrize("threshold", [1, 2])
def test_threshold_is_honoured_exactly(tmp_path, threshold):
    downloader = _make_downloader(tmp_path, slots=2)

    with patch.object(downloader_module, "URL_SLOT_FAILURE_THRESHOLD", threshold):
        for _ in range(threshold - 1):
            downloader._note_url_slot_failure(0)
            assert 0 not in downloader._url_slot_cooldown_until
        downloader._note_url_slot_failure(0)

    assert 0 in downloader._url_slot_cooldown_until
