"""Tests for R2-11's throughput telemetry: _report_progress's is_direct
bookkeeping and _maybe_report_throughput's periodic status message.

Motivation (see docs/ai/todolist.md R2-11): the working hypothesis is that
this project's effective 1~3MB/s throughput comes from a per-connection
(not per-IP) rate limit, and that direct-connection chunks likely carry
most of the traffic (only one chunk can ever be on the direct slot at a
time -- see _acquire_proxy_lock). There was previously no way to observe
either the aggregate speed or the direct-vs-proxy split without external
packet capture; this is the instrumentation needed to let real usage
validate (or refute) that hypothesis -- it does not itself prove or
disprove anything without live Keep2Share traffic.
"""
from __future__ import annotations

from unittest.mock import patch

from tqdm import tqdm

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import Downloader, _DownloadContext


def _make_downloader():
    return Downloader()


def _make_ctx():
    return _DownloadContext(
        urls=["https://example.com/f"],
        filename="out.bin",
        headers={"User-Agent": "test-agent"},
        threads=1,
        split_count=1,
        progress_bar=tqdm(total=1, disable=True),
        file_id="test-file-id",
        total_size=0,
        bytes_per_split=0,
        ranges={},
    )


class TestReportProgressConnectionTypeBookkeeping:
    def test_is_direct_true_credits_direct_bytes_only(self):
        downloader = _make_downloader()
        ctx = _make_ctx()

        downloader._report_progress(100, ctx, is_direct=True)

        assert downloader._direct_bytes_downloaded == 100
        assert downloader._proxy_bytes_downloaded == 0
        assert downloader._bytes_downloaded == 100

    def test_is_direct_false_credits_proxy_bytes_only(self):
        downloader = _make_downloader()
        ctx = _make_ctx()

        downloader._report_progress(100, ctx, is_direct=False)

        assert downloader._direct_bytes_downloaded == 0
        assert downloader._proxy_bytes_downloaded == 100
        assert downloader._bytes_downloaded == 100

    def test_is_direct_none_credits_neither_but_still_updates_total(self):
        # This is the scheduling loop's part-reuse branch's case: bytes
        # from an already-on-disk part were never pulled over a live
        # connection this run, so they must not skew the direct/proxy
        # throughput split.
        downloader = _make_downloader()
        ctx = _make_ctx()

        downloader._report_progress(100, ctx)

        assert downloader._direct_bytes_downloaded == 0
        assert downloader._proxy_bytes_downloaded == 0
        assert downloader._bytes_downloaded == 100

    def test_negative_delta_rolls_back_the_same_bucket(self):
        downloader = _make_downloader()
        ctx = _make_ctx()

        downloader._report_progress(100, ctx, is_direct=True)
        downloader._report_progress(-40, ctx, is_direct=True)

        assert downloader._direct_bytes_downloaded == 60
        assert downloader._bytes_downloaded == 60


class TestMaybeReportThroughput:
    def _downloader_with_messages(self):
        downloader = _make_downloader()
        messages: list[str] = []
        downloader.status_callback = messages.append
        return downloader, messages

    def test_no_message_before_interval_elapses(self):
        downloader, messages = self._downloader_with_messages()
        downloader._telemetry_last_emit_time = 1000.0

        with patch.object(downloader_module.time, "time", return_value=1000.0 + downloader_module.TELEMETRY_REPORT_INTERVAL - 0.1):
            downloader._maybe_report_throughput()

        assert messages == []

    def test_no_message_when_no_bytes_moved(self):
        downloader, messages = self._downloader_with_messages()
        downloader._telemetry_last_emit_time = 1000.0

        with patch.object(downloader_module.time, "time", return_value=1000.0 + downloader_module.TELEMETRY_REPORT_INTERVAL + 1):
            downloader._maybe_report_throughput()

        assert messages == []

    def test_reports_speed_and_direct_proxy_split_after_interval(self):
        downloader, messages = self._downloader_with_messages()
        downloader._telemetry_last_emit_time = 1000.0
        ctx = _make_ctx()
        downloader._report_progress(700, ctx, is_direct=True)
        downloader._report_progress(300, ctx, is_direct=False)

        with patch.object(downloader_module.time, "time", return_value=1010.0):
            downloader._maybe_report_throughput()

        assert len(messages) == 1
        assert "Throughput" in messages[0]
        assert "direct: 70%" in messages[0]
        assert "proxy: 30%" in messages[0]

    def test_reports_resumed_only_case_without_a_split(self):
        downloader, messages = self._downloader_with_messages()
        downloader._telemetry_last_emit_time = 1000.0
        ctx = _make_ctx()
        # Credited via the part-reuse branch (is_direct=None): counts
        # toward total bytes/speed but not the direct/proxy split.
        downloader._report_progress(500, ctx)

        with patch.object(downloader_module.time, "time", return_value=1010.0):
            downloader._maybe_report_throughput()

        assert len(messages) == 1
        assert "resumed chunks only" in messages[0]

    def test_only_reports_the_interval_delta_not_cumulative_totals(self):
        downloader, messages = self._downloader_with_messages()
        downloader._telemetry_last_emit_time = 1000.0
        ctx = _make_ctx()
        downloader._report_progress(1000, ctx, is_direct=True)

        with patch.object(downloader_module.time, "time", return_value=1010.0):
            downloader._maybe_report_throughput()
        assert len(messages) == 1

        # A second interval with only proxy traffic: the first message's
        # all-direct history must not bleed into this one.
        downloader._report_progress(1000, ctx, is_direct=False)
        with patch.object(downloader_module.time, "time", return_value=1020.0):
            downloader._maybe_report_throughput()

        assert len(messages) == 2
        assert "direct: 100%" in messages[0]
        assert "proxy: 100%" in messages[1]

    def test_active_connection_count_is_included(self):
        downloader, messages = self._downloader_with_messages()
        downloader._telemetry_last_emit_time = 1000.0
        downloader._active_proxy_indexes = {0, 2}
        ctx = _make_ctx()
        downloader._report_progress(100, ctx, is_direct=True)

        with patch.object(downloader_module.time, "time", return_value=1010.0):
            downloader._maybe_report_throughput()

        assert "2 active connection(s)" in messages[0]
