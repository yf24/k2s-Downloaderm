"""Tests for P4-3: human_readable_bytes must use IEC binary unit labels.

The function divides by 1024 at each step, so the labels must be the binary
KiB/MiB/GiB/TiB/PiB names (matching gui/main_window.py's _format_speed),
not the decimal SI names KB/MB/GB/TB it previously used despite the binary
math.
"""
from __future__ import annotations

import pytest

from k2s_downloader.core.downloader import human_readable_bytes


class TestHumanReadableBytes:
    @pytest.mark.parametrize(
        "num,expected_unit",
        [
            (0, "bytes"),
            (1023, "bytes"),
            (1024, "KiB"),
            (2**20, "MiB"),
            (2**30, "GiB"),
            (2**40, "TiB"),
            (2**50, "PiB"),
        ],
    )
    def test_uses_iec_binary_unit_labels(self, num, expected_unit):
        result = human_readable_bytes(num)
        assert result.endswith(expected_unit)
        # Old decimal-sounding labels must not appear anywhere in the output.
        for decimal_label in ("KB", "MB", "GB", "TB", "PB"):
            assert decimal_label not in result

    def test_value_scales_correctly_within_a_unit(self):
        assert human_readable_bytes(1536) == "1.500 KiB"
