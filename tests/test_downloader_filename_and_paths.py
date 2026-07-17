"""Tests for R2-4 and R2-5: Windows-safe filenames and path handling.

- R2-4: `original_name` (as returned by `k2s_client.get_name`, from the
  Keep2Share API -- untrusted) and any filename component supplied by the
  caller must be sanitized before touching the filesystem. Without this,
  Windows-illegal characters or reserved device names turned a plain
  `OSError` from `Path.write_bytes` into a misleading "IP/proxy blocked"
  error after 8 pointless retries.
- R2-5: a `filename` with directory components (e.g. CLI
  `--filename out/video.mp4`) must not change where part files land --
  they always stay flat under `tmp_dir` -- and `_merge_parts` must create
  the final output's parent directory if it doesn't exist yet.
"""
from __future__ import annotations

from pathlib import Path

from k2s_downloader.core.downloader import Downloader, _sanitize_filename_component


class TestSanitizeFilenameComponent:
    def test_illegal_windows_characters_are_replaced(self):
        assert _sanitize_filename_component('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"

    def test_control_characters_are_replaced(self):
        assert _sanitize_filename_component("a\x00b\x1fc") == "a_b_c"

    def test_reserved_device_name_is_prefixed(self):
        assert _sanitize_filename_component("CON") == "_CON"
        assert _sanitize_filename_component("con.mp4") == "_con.mp4"
        assert _sanitize_filename_component("LPT1") == "_LPT1"

    def test_reserved_name_check_is_case_insensitive(self):
        assert _sanitize_filename_component("Nul") == "_Nul"

    def test_non_reserved_name_with_reserved_prefix_is_untouched(self):
        # "console.mp4"'s stem is "console", not the reserved "CONSOLE" --
        # must not be mistaken for the reserved device name "CON".
        assert _sanitize_filename_component("console.mp4") == "console.mp4"

    def test_trailing_dots_and_spaces_are_stripped(self):
        assert _sanitize_filename_component("video.mp4. ") == "video.mp4"
        assert _sanitize_filename_component("video.mp4 . . ") == "video.mp4"

    def test_leading_and_trailing_whitespace_is_stripped(self):
        assert _sanitize_filename_component("  video.mp4  ") == "video.mp4"

    def test_empty_or_all_illegal_input_falls_back_to_default(self):
        assert _sanitize_filename_component("") == "download"
        assert _sanitize_filename_component("...") == "download"
        # "///" is legal once slashes become underscores ("___"), so it is
        # not this fallback case -- only inputs that sanitize to nothing
        # (or only trailing dots/spaces) hit the "download" default.
        assert _sanitize_filename_component("   ") == "download"

    def test_ordinary_name_is_unchanged(self):
        assert _sanitize_filename_component("My Video (2024).mp4") == "My Video (2024).mp4"

    def test_path_separators_cannot_smuggle_in_directory_traversal(self):
        result = _sanitize_filename_component("../../etc/evil.mp4")
        assert "/" not in result
        assert "\\" not in result


class TestResolveFilenameSanitizesServerName:
    def test_server_name_with_illegal_characters_is_sanitized(self):
        resolved = Downloader._resolve_filename(None, 'bad:name?.mp4')
        assert resolved == "bad_name_.mp4"

    def test_server_name_reserved_device_name_is_sanitized(self):
        resolved = Downloader._resolve_filename(None, "con.mp4")
        assert resolved == "_con.mp4"

    def test_ordinary_server_name_passes_through(self):
        assert Downloader._resolve_filename(None, "video.mp4") == "video.mp4"


class TestResolveFilenameUserSuppliedName:
    def test_user_filename_illegal_characters_are_sanitized(self):
        resolved = Downloader._resolve_filename('bad:name?.mp4', "original.mp4")
        assert resolved == "bad_name_.mp4"

    def test_user_filename_without_suffix_gets_sanitized_original_suffix(self):
        resolved = Downloader._resolve_filename("my<video>", "original.mp4")
        assert resolved == "my_video_.mp4"

    def test_directory_components_in_user_filename_are_preserved(self):
        resolved = Downloader._resolve_filename("out/video.mp4", "original.mkv")
        assert Path(resolved) == Path("out/video.mp4")

    def test_directory_components_preserved_when_suffix_comes_from_original(self):
        resolved = Downloader._resolve_filename("out/myvideo", "original.mkv")
        assert Path(resolved) == Path("out/myvideo.mkv")

    def test_only_the_final_path_component_is_sanitized(self):
        # The illegal character lives in the basename, not the directory;
        # only the basename should be touched.
        resolved = Downloader._resolve_filename("out/bad<name>.mp4", "original.mp4")
        assert Path(resolved) == Path("out/bad_name_.mp4")


class TestApplyOutputDir:
    def test_none_output_dir_leaves_resolved_name_untouched(self):
        assert Downloader._apply_output_dir("out/video.mp4", None) == "out/video.mp4"

    def test_output_dir_is_joined_with_basename(self, tmp_path):
        result = Downloader._apply_output_dir("video.mp4", tmp_path / "Downloads")
        assert Path(result) == tmp_path / "Downloads" / "video.mp4"

    def test_output_dir_strips_any_directory_component_from_resolved_name(self, tmp_path):
        # A `--filename out/video.mp4`-style directory component must not
        # escape the caller-chosen output_dir (R2-9's GUI "save to" picker).
        result = Downloader._apply_output_dir("out/nested/video.mp4", tmp_path / "Downloads")
        assert Path(result) == tmp_path / "Downloads" / "video.mp4"


class TestPartPathStaysFlatUnderTmpDir:
    def test_part_path_uses_only_the_basename(self, tmp_path):
        downloader = Downloader(tmp_dir=tmp_path / "tmp")
        # split_count=10 -> 2-digit zero-padded index, matching the
        # existing zfill(len(str(split_count))) convention.
        part_path = downloader._part_path("out/video.mp4", 0, 10)
        assert part_path == tmp_path / "tmp" / "video.mp4.part00"
        assert part_path.parent == downloader.tmp_dir

    def test_part_path_unaffected_by_absolute_filename(self, tmp_path):
        downloader = Downloader(tmp_dir=tmp_path / "tmp")
        absolute_filename = str(tmp_path / "somewhere" / "out.bin")
        part_path = downloader._part_path(absolute_filename, 3, 10)
        assert part_path == tmp_path / "tmp" / "out.bin.part03"


class TestMergePartsCreatesTargetParentDirectory:
    def test_merge_parts_creates_missing_parent_directory(self, tmp_path):
        downloader = Downloader(tmp_dir=tmp_path / "tmp")
        downloader.tmp_dir.mkdir(parents=True)

        target = tmp_path / "out" / "nested" / "video.bin"
        part_bytes = [b"hello ", b"world"]
        ranges = {}
        for idx, chunk in enumerate(part_bytes):
            part_path = downloader._part_path(str(target), idx, len(part_bytes))
            part_path.write_bytes(chunk)
            ranges[str(idx)] = {"bytes": len(chunk)}

        result_path = downloader._merge_parts(ranges, str(target))

        assert result_path == target
        assert target.read_bytes() == b"hello world"

    def test_merge_parts_uses_basename_for_part_files_not_full_path(self, tmp_path):
        downloader = Downloader(tmp_dir=tmp_path / "tmp")
        downloader.tmp_dir.mkdir(parents=True)

        target = tmp_path / "sub" / "video.bin"
        part_path = downloader.tmp_dir / "video.bin.part0"
        part_path.write_bytes(b"payload")

        result_path = downloader._merge_parts({"0": {"bytes": len(b"payload")}}, str(target))

        assert result_path.read_bytes() == b"payload"
        assert not part_path.exists()  # consumed during merge
