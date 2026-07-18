"""Tests for R2-7 and R2-13: streaming chunk writes and resumable downloads.

- R2-7: a chunk used to be buffered whole in memory (``io.BytesIO``) and only
  touch disk once, after the entire response had been read; ``_merge_parts``
  similarly read each whole part file into memory before writing it out.
  Both are now streamed: a chunk is written incrementally to a ``.partNN.tmp``
  file and atomically renamed to ``.partNN`` only once its full expected size
  is confirmed, and ``_merge_parts`` uses ``shutil.copyfileobj``.
- R2-13: a resume manifest (``<filename>.manifest.json`` under ``tmp_dir``)
  records which ranges are actually complete, gated on ``file_id``/
  ``total_size``/split layout all matching. This is what lets a later run
  tell "my own leftover part files" apart from a stale/foreign set that
  merely happens to share this filename and byte counts -- the old
  size-only part-reuse check (still the mechanism that *performs* the
  reuse) had no way to make that distinction on its own.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from k2s_downloader.core import downloader as downloader_module
from k2s_downloader.core.downloader import ChunkDownloadFailed, Downloader, ResumeProgress


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


def _response(status_code: int, iter_factory):
    response = MagicMock()
    response.status_code = status_code
    response.iter_content.side_effect = iter_factory
    return response


class TestChunkStreamsIncrementallyToTmpThenRenames:
    def test_partial_data_is_flushed_to_the_tmp_file_before_the_chunk_finishes(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body_parts = [b"AAAAA", b"BBBBB"]
        total_body = b"".join(body_parts)
        tmp_part_path = downloader.tmp_dir / "out.bin.part0.tmp"
        final_part_path = downloader.tmp_dir / "out.bin.part0"
        observed = {}

        def iter_content(block_size):
            yield body_parts[0]
            # The old implementation buffered the whole chunk in RAM and
            # only ever touched disk once, at the very end -- by the time
            # the second block is pulled, the first block's bytes must
            # already be on disk in the .tmp file, and the final .partNN
            # name must not exist yet (rename only happens after the whole
            # chunk is confirmed complete).
            observed["tmp_contents_mid_stream"] = (
                tmp_part_path.read_bytes() if tmp_part_path.exists() else None
            )
            observed["final_exists_mid_stream"] = final_part_path.exists()
            yield body_parts[1]

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(total_body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            return_value=_response(206, iter_content),
        ):
            result_path = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=len(total_body),
            )

        assert observed["tmp_contents_mid_stream"] == body_parts[0]
        assert observed["final_exists_mid_stream"] is False
        assert not tmp_part_path.exists()  # renamed away, not left behind
        # A single-range download merges (and deletes the part file)
        # immediately on success, so the completed data now lives at the
        # final output path rather than at final_part_path.
        assert result_path.read_bytes() == total_body

    def test_incomplete_tmp_file_is_never_mistaken_for_a_complete_part(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body = b"hello"
        # A manifest that matches this run authorizes resume in general,
        # but explicitly records range 0 as *not yet* downloaded (as if the
        # process died mid-chunk, after the .tmp was partially written but
        # before it was renamed). A stray .tmp with the right final byte
        # count sitting around must still not fool the scheduling loop's
        # part-reuse branch, which only ever looks at the final (non-.tmp)
        # name -- if it did, this range would be silently skipped with
        # truncated/corrupt data instead of being (re)downloaded.
        stray_tmp = downloader.tmp_dir / "out.bin.part0.tmp"
        stray_tmp.write_bytes(body)
        manifest_path = downloader.tmp_dir / "out.bin.manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "file_id": "file-abc",
                    "total_size": len(body),
                    "split_size": len(body),
                    "split_count": 1,
                    "ranges": {"0": {"range": f"0-{len(body) - 1}", "bytes": len(body), "downloaded": False}},
                }
            )
        )

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            return_value=_response(200, lambda block_size: iter([body])),
        ) as mock_get:
            result_path = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=len(body),
                file_id="file-abc",
            )

        assert mock_get.called  # a real download attempt was made, not skipped
        assert result_path.read_bytes() == body
        assert stray_tmp.exists() is False  # overwritten by the real attempt's own .tmp write

    def test_failed_attempt_does_not_leave_a_partial_tmp_file_behind(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body = b"x"
        tmp_part_path = downloader.tmp_dir / "out.bin.part0.tmp"

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            return_value=_response(200, lambda block_size: iter([b"short"])),
        ):
            with pytest.raises(ChunkDownloadFailed, match="size mismatch"):
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=len(body),
                )

        assert not tmp_part_path.exists()


class TestMergePartsStreamsInsteadOfBuffering:
    def test_merge_produces_byte_identical_output_across_multiple_parts(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        parts = [b"0123456789", b"ABCDEFGHIJ", b"zyxwvutsrq"]
        total_body = b"".join(parts)
        call_log = []

        def get_side_effect(*args, **kwargs):
            range_header = kwargs["headers"]["Range"]
            call_log.append(range_header)
            start, end = (int(x) for x in range_header.removeprefix("bytes=").split("-"))
            return _response(206, lambda block_size, s=start, e=end: iter([total_body[s : e + 1]]))

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(total_body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            side_effect=get_side_effect,
        ):
            result_path = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=10,
            )

        assert result_path.read_bytes() == total_body
        assert len(call_log) == 3
        assert not list(downloader.tmp_dir.glob("*.part*"))  # all parts consumed


class TestResumeManifest:
    def _run_two_range_download_with_second_range_blocked(self, downloader, tmp_path, file_id, total_body):
        """First 5 bytes succeed; second 5 bytes are permanently blocked
        (MAX_CHUNK_RETRIES=1), leaving one completed part + a manifest."""

        def get_side_effect(*args, **kwargs):
            range_header = kwargs["headers"]["Range"]
            if range_header == "bytes=0-4":
                return _response(200, lambda block_size: iter([total_body[0:5]]))
            return _response(403, lambda block_size: iter([]))

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(total_body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            side_effect=get_side_effect,
        ):
            with pytest.raises(ChunkDownloadFailed):
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=5,
                    file_id=file_id,
                )

    def test_manifest_is_written_with_expected_schema_after_partial_progress(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        total_body = b"HELLOWORLD"  # 10 bytes -> two 5-byte ranges
        self._run_two_range_download_with_second_range_blocked(downloader, tmp_path, "file-abc", total_body)

        manifest_path = downloader.tmp_dir / "out.bin.manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())

        assert manifest["file_id"] == "file-abc"
        assert manifest["total_size"] == 10
        assert manifest["split_size"] == 5
        assert manifest["split_count"] == 2
        assert manifest["ranges"]["0"] == {"range": "0-4", "bytes": 5, "downloaded": True}
        assert manifest["ranges"]["1"]["downloaded"] is False
        assert (downloader.tmp_dir / "out.bin.part0").read_bytes() == total_body[0:5]
        assert not (downloader.tmp_dir / "out.bin.part1").exists()

    def test_manifest_is_deleted_after_a_fully_successful_download(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        body = b"complete"

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            return_value=_response(200, lambda block_size: iter([body])),
        ):
            downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=len(body),
                file_id="file-abc",
            )

        assert not (downloader.tmp_dir / "out.bin.manifest.json").exists()

    def test_resume_skips_already_completed_ranges_when_manifest_matches(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        total_body = b"HELLOWORLD"
        self._run_two_range_download_with_second_range_blocked(downloader, tmp_path, "file-abc", total_body)
        # download() clears stop_event at the start of every run; these
        # tests call _download_once directly (simulating a fresh process
        # picking the same tmp_dir back up), so do the same by hand.
        downloader.stop_event.clear()

        messages: list[str] = []
        downloader.status_callback = messages.append
        requested_ranges: list[str] = []

        def get_side_effect(*args, **kwargs):
            range_header = kwargs["headers"]["Range"]
            requested_ranges.append(range_header)
            start, end = (int(x) for x in range_header.removeprefix("bytes=").split("-"))
            return _response(200, lambda block_size, s=start, e=end: iter([total_body[s : e + 1]]))

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(total_body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            side_effect=get_side_effect,
        ):
            result_path = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=5,
                file_id="file-abc",
            )

        # The already-complete first range must not be re-fetched over the
        # network -- only the previously-blocked second range should be.
        assert requested_ranges == ["bytes=5-9"]
        assert result_path.read_bytes() == total_body
        assert any("resuming" in m.lower() and "1/2" in m for m in messages)

    def test_resume_is_rejected_and_stale_parts_cleared_when_file_id_differs(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        old_body = b"OLDSTALE12"  # 10 bytes -> two 5-byte ranges, like new_body below
        self._run_two_range_download_with_second_range_blocked(downloader, tmp_path, "file-original", old_body)
        # download() clears stop_event at the start of every run; this test
        # calls _download_once directly, so do the same by hand.
        downloader.stop_event.clear()
        # Simulate a *different* Keep2Share file that happens to resolve to
        # the same output filename, total size, and split layout as the one
        # above (same tmp_dir + filename collision). Its own data must win;
        # the stale part-0 bytes belong to an unrelated download and must
        # not be spliced in just because the byte count happens to match.
        new_body = b"FRESHDATA!"
        assert len(new_body) == len(old_body)

        requested_ranges: list[str] = []

        def get_side_effect(*args, **kwargs):
            range_header = kwargs["headers"]["Range"]
            requested_ranges.append(range_header)
            start, end = (int(x) for x in range_header.removeprefix("bytes=").split("-"))
            return _response(200, lambda block_size, s=start, e=end: iter([new_body[s : e + 1]]))

        messages: list[str] = []
        downloader.status_callback = messages.append

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(new_body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            side_effect=get_side_effect,
        ):
            result_path = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=5,
                file_id="file-different",
            )

        # Both ranges must have been genuinely (re)downloaded from the
        # network -- the stale part-0 was not silently reused.
        assert requested_ranges == ["bytes=0-4", "bytes=5-9"]
        assert result_path.read_bytes() == new_body
        assert any("leftover" in m.lower() or "clearing" in m.lower() for m in messages)

    def test_resume_falls_through_to_fresh_download_when_matching_part_file_is_missing(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        total_body = b"HELLOWORLD"
        self._run_two_range_download_with_second_range_blocked(downloader, tmp_path, "file-abc", total_body)
        # download() clears stop_event at the start of every run; this test
        # calls _download_once directly, so do the same by hand.
        downloader.stop_event.clear()
        # The manifest still says range 0 is downloaded, but the actual
        # part file is gone (e.g. the user deleted the temp folder by
        # hand) -- the manifest alone must never be trusted for the data.
        (downloader.tmp_dir / "out.bin.part0").unlink()

        requested_ranges: list[str] = []

        def get_side_effect(*args, **kwargs):
            range_header = kwargs["headers"]["Range"]
            requested_ranges.append(range_header)
            start, end = (int(x) for x in range_header.removeprefix("bytes=").split("-"))
            return _response(200, lambda block_size, s=start, e=end: iter([total_body[s : e + 1]]))

        with patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(total_body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            side_effect=get_side_effect,
        ):
            result_path = downloader._download_once(
                ["https://example.com/f"],
                str(tmp_path / "out.bin"),
                threads=1,
                bytes_per_split=5,
                file_id="file-abc",
            )

        assert requested_ranges == ["bytes=0-4", "bytes=5-9"]
        assert result_path.read_bytes() == total_body


class TestFindResumeProgress:
    """Tests for the GUI "previous progress" preview: a lookup by file_id
    (extractable from the URL alone, no API call) that scans tmp_dir for a
    matching manifest, so the GUI can show "you're already 65% through this
    file" before a download is even started."""

    def test_returns_none_when_tmp_dir_does_not_exist(self, tmp_path):
        assert Downloader.find_resume_progress(tmp_path / "does-not-exist", "file-abc") is None

    def test_returns_none_when_no_manifest_matches_file_id(self, tmp_path):
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        (tmp_dir / "out.bin.manifest.json").write_text(json.dumps({
            "file_id": "other-file",
            "total_size": 10,
            "ranges": {"0": {"range": "0-4", "bytes": 5, "downloaded": True}},
        }))

        assert Downloader.find_resume_progress(tmp_dir, "file-abc") is None

    def test_finds_matching_manifest_and_computes_percent(self, tmp_path):
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        (tmp_dir / "My Video.mp4.manifest.json").write_text(json.dumps({
            "file_id": "file-abc",
            "total_size": 100,
            "split_size": 50,
            "split_count": 2,
            "ranges": {
                "0": {"range": "0-49", "bytes": 50, "downloaded": True},
                "1": {"range": "50-99", "bytes": 50, "downloaded": False},
            },
        }))

        result = Downloader.find_resume_progress(tmp_dir, "file-abc")

        assert result == ResumeProgress(
            filename="My Video.mp4", downloaded_bytes=50, total_size=100, percent=50.0
        )

    def test_ignores_corrupt_manifest_files(self, tmp_path):
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        (tmp_dir / "out.bin.manifest.json").write_text("not json{{{")

        assert Downloader.find_resume_progress(tmp_dir, "file-abc") is None

    def test_ignores_manifest_missing_required_fields(self, tmp_path):
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        (tmp_dir / "out.bin.manifest.json").write_text(json.dumps({"file_id": "file-abc"}))

        assert Downloader.find_resume_progress(tmp_dir, "file-abc") is None

    def test_real_manifest_written_by_a_download_is_found(self, tmp_path):
        # End-to-end: exercise the real manifest-writing path (not a
        # hand-built fixture) to make sure the schema _persist_manifest
        # writes is exactly what find_resume_progress expects.
        downloader = _make_downloader(tmp_path)
        total_body = b"HELLOWORLD"

        def get_side_effect(*args, **kwargs):
            range_header = kwargs["headers"]["Range"]
            if range_header == "bytes=0-4":
                return _response(200, lambda block_size: iter([total_body[0:5]]))
            return _response(403, lambda block_size: iter([]))

        with patch.object(downloader_module, "MAX_CHUNK_RETRIES", 1), patch(
            "k2s_downloader.core.downloader.requests.head",
            return_value=_head(len(total_body)),
        ), patch(
            "k2s_downloader.core.downloader.requests.get",
            side_effect=get_side_effect,
        ):
            with pytest.raises(ChunkDownloadFailed):
                downloader._download_once(
                    ["https://example.com/f"],
                    str(tmp_path / "out.bin"),
                    threads=1,
                    bytes_per_split=5,
                    file_id="file-abc",
                )

        result = Downloader.find_resume_progress(downloader.tmp_dir, "file-abc")

        assert result.filename == "out.bin"
        assert result.downloaded_bytes == 5
        assert result.total_size == 10
        assert result.percent == 50.0
