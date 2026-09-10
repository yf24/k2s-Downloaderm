"""Tests for R3-1..R3-3: the byte-level integrity checks around a download.

Before this round the only proof a chunk was correct was its length, and even
that was fuzzy (``math.isclose(..., abs_tol=1)``): a segment that arrived one
byte short was accepted, which shifts every following byte of the merged file
-- a video that still plays but an archive that can never be extracted. Three
gaps are closed here:

- R3-1: byte counts must match exactly, and the count is taken from the part
  file itself as well as from the running counter.
- R3-2: a success response must describe the range that was actually asked
  for (``Content-Range`` on a 206; a 200 means ``Range`` was ignored, which is
  the wrong data for every chunk of a split download).
- R3-3: every part is re-verified before the merge starts, and the merged file
  is verified afterwards, so nothing that damaged a part after it landed can
  reach the output silently.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import ChunkDownloadFailed, DownloadIntegrityError, Downloader

GET_TARGET = "k2s_downloader.core.downloader.requests.get"
HEAD_TARGET = "k2s_downloader.core.downloader.requests.head"


def _make_downloader(tmp_path):
    downloader = Downloader(
        tmp_dir=tmp_path / "tmp",
        url_cache_path=tmp_path / "urls.json",
        block_size=1,
    )
    downloader.tmp_dir.mkdir(parents=True, exist_ok=True)
    downloader.proxies = [None]
    downloader.proxy_locks = [threading.Lock()]
    downloader.working_proxy_indexes = []
    return downloader


def _head(total: int) -> MagicMock:
    head = MagicMock()
    head.headers = {"Content-Length": str(total)}
    return head


def _response(status_code: int, body: bytes, content_range: Optional[str] = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = {} if content_range is None else {"Content-Range": content_range}
    response.iter_content.side_effect = lambda block_size: iter([body])
    return response


def _requested_bounds(kwargs) -> tuple:
    first, last = kwargs["headers"]["Range"].split("=")[1].split("-")
    return int(first), int(last)


def _download_once(downloader, tmp_path, *, bytes_per_split: int):
    return downloader._download_once(
        ["https://example.com/f"],
        str(tmp_path / "out.bin"),
        threads=1,
        bytes_per_split=bytes_per_split,
        file_id="file-abc",
    )


class TestChunkByteCountMustMatchExactly:
    """R3-1: ``abs_tol=1`` used to wave through an off-by-one segment."""

    @pytest.mark.parametrize("body", [b"012345678", b"01234567890"], ids=["one-short", "one-long"])
    def test_off_by_one_chunk_is_rejected(self, tmp_path, body):
        downloader = _make_downloader(tmp_path)

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), patch(
            HEAD_TARGET, return_value=_head(10)
        ), patch(GET_TARGET, return_value=_response(200, body)):
            with pytest.raises(ChunkDownloadFailed, match="size mismatch"):
                _download_once(downloader, tmp_path, bytes_per_split=10)

        assert not (tmp_path / "out.bin").exists()
        assert not list(downloader.tmp_dir.glob("*.part*"))

    def test_exactly_sized_chunk_is_accepted(self, tmp_path):
        downloader = _make_downloader(tmp_path)

        with patch(HEAD_TARGET, return_value=_head(10)), patch(
            GET_TARGET, return_value=_response(200, b"0123456789")
        ):
            result = _download_once(downloader, tmp_path, bytes_per_split=10)

        assert result.read_bytes() == b"0123456789"

    def test_size_is_taken_from_the_part_file_not_just_the_counter(self, tmp_path):
        """The running counter only proves what was handed to ``write()``."""
        downloader = _make_downloader(tmp_path)
        real_open = Path.open

        def dropping_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if self.name.endswith(".part0.tmp"):
                original_write = handle.write

                def write_one_byte_less(data):
                    original_write(data[:-1])
                    return len(data)

                handle.write = write_one_byte_less
            return handle

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), patch(
            HEAD_TARGET, return_value=_head(4)
        ), patch(GET_TARGET, return_value=_response(200, b"ABCD")), patch.object(
            Path, "open", dropping_open
        ):
            with pytest.raises(ChunkDownloadFailed, match="on disk"):
                _download_once(downloader, tmp_path, bytes_per_split=4)


class TestServedRangeMustMatchTheRequestedRange:
    """R3-2: right length, wrong offset used to be indistinguishable."""

    def _split_download(self, downloader, tmp_path, get_side_effect):
        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), patch(
            HEAD_TARGET, return_value=_head(10)
        ), patch(GET_TARGET, side_effect=get_side_effect):
            return _download_once(downloader, tmp_path, bytes_per_split=5)

    def test_206_describing_a_different_window_is_rejected(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body = b"HELLOWORLD"

        def get_side_effect(*args, **kwargs):
            # Both ranges are answered with the file's first half: the right
            # length for either, but the wrong bytes for the second.
            return _response(206, body[0:5], content_range="bytes 0-4/10")

        with pytest.raises(ChunkDownloadFailed, match="served bytes 0-4 instead of 5-9"):
            self._split_download(downloader, tmp_path, get_side_effect)

    def test_matching_content_range_is_accepted(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body = b"HELLOWORLD"

        def get_side_effect(*args, **kwargs):
            first, last = _requested_bounds(kwargs)
            return _response(206, body[first : last + 1], content_range=f"bytes {first}-{last}/10")

        result = self._split_download(downloader, tmp_path, get_side_effect)
        assert result.read_bytes() == body

    def test_206_without_a_readable_content_range_falls_back_to_the_byte_count(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body = b"HELLOWORLD"

        def get_side_effect(*args, **kwargs):
            first, last = _requested_bounds(kwargs)
            return _response(206, body[first : last + 1], content_range="bytes */10")

        result = self._split_download(downloader, tmp_path, get_side_effect)
        assert result.read_bytes() == body

    def test_200_on_a_split_download_is_rejected(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body = b"HELLOWORLD"

        def get_side_effect(*args, **kwargs):
            first, last = _requested_bounds(kwargs)
            return _response(200, body[first : last + 1])

        with pytest.raises(ChunkDownloadFailed, match="ignored the Range request"):
            self._split_download(downloader, tmp_path, get_side_effect)

    def test_200_on_a_single_range_download_is_still_accepted(self, tmp_path):
        """One range means the whole file *is* the requested range anyway."""
        downloader = _make_downloader(tmp_path)

        with patch(HEAD_TARGET, return_value=_head(10)), patch(
            GET_TARGET, return_value=_response(200, b"HELLOWORLD")
        ):
            result = _download_once(downloader, tmp_path, bytes_per_split=10)

        assert result.read_bytes() == b"HELLOWORLD"


class TestPartsAreVerifiedAroundTheMerge:
    """R3-3: the merge used to trust whatever part files it found."""

    def _prepare(self, tmp_path, part_bodies):
        downloader = _make_downloader(tmp_path)
        filename = str(tmp_path / "out.bin")
        ranges = Downloader._build_ranges(10, 2)
        for idx, body in enumerate(part_bodies):
            if body is not None:
                downloader._part_path(filename, idx, 2).write_bytes(body)
        manifest_path = downloader._manifest_path(filename)
        manifest_path.write_text(json.dumps({"file_id": "file-abc"}), encoding="utf-8")
        return downloader, filename, ranges, manifest_path

    def test_wrong_sized_part_aborts_the_merge_and_keeps_the_resume_state(self, tmp_path):
        downloader, filename, ranges, manifest_path = self._prepare(tmp_path, [b"HELLO", b"WORL"])

        with pytest.raises(DownloadIntegrityError, match="Segment 1 of 2 is 4 bytes"):
            downloader._merge_parts(ranges, filename)

        assert downloader._part_path(filename, 0, 2).exists()
        assert downloader._part_path(filename, 1, 2).exists()
        assert manifest_path.exists()
        assert not Path(filename).exists()

    def test_missing_part_aborts_the_merge(self, tmp_path):
        downloader, filename, ranges, manifest_path = self._prepare(tmp_path, [b"HELLO", None])

        with pytest.raises(DownloadIntegrityError, match="Segment 1 of 2 is -1 bytes"):
            downloader._merge_parts(ranges, filename)

        assert downloader._part_path(filename, 0, 2).exists()
        assert manifest_path.exists()

    def test_correct_parts_merge_and_are_consumed(self, tmp_path):
        downloader, filename, ranges, manifest_path = self._prepare(tmp_path, [b"HELLO", b"WORLD"])

        result = downloader._merge_parts(ranges, filename)

        assert result.read_bytes() == b"HELLOWORLD"
        assert not list(downloader.tmp_dir.glob("*.part*"))
        assert not manifest_path.exists()

    def test_short_merged_output_is_removed_instead_of_being_handed_back(self, tmp_path):
        downloader, filename, ranges, _ = self._prepare(tmp_path, [b"HELLO", b"WORLD"])

        def short_copy(src, dst, *args, **kwargs):
            dst.write(src.read()[:-1])

        with patch.object(downloader_module, "copyfileobj", short_copy):
            with pytest.raises(DownloadIntegrityError, match="Merged file is 8 bytes"):
                downloader._merge_parts(ranges, filename)

        assert not Path(filename).exists()


class TestResumeRequiresAnExactlySizedPart:
    """R3-1 again, on the path that decides what a previous run finished."""

    def test_manifest_backed_part_one_byte_short_is_not_credited(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        filename = str(tmp_path / "out.bin")
        ranges = Downloader._build_ranges(10, 2)
        downloader._part_path(filename, 0, 2).write_bytes(b"HELL")  # should be 5 bytes
        downloader._manifest_path(filename).write_text(
            json.dumps(
                {
                    "file_id": "file-abc",
                    "total_size": 10,
                    "split_size": 5,
                    "split_count": 2,
                    "ranges": {
                        "0": {"range": "0-4", "bytes": 5, "downloaded": True},
                        "1": {"range": "5-9", "bytes": 5, "downloaded": False},
                    },
                }
            ),
            encoding="utf-8",
        )

        downloader._prepare_resume(filename, "file-abc", 10, 5, ranges)

        assert not ranges["0"]["downloaded"]
        assert downloader._done_count == 0
        assert downloader._bytes_downloaded == 0
