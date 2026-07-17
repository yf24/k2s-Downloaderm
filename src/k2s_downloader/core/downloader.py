from __future__ import annotations

import json
import math
import os
import random
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

import requests
from shutil import copyfileobj, which
from tqdm import tqdm

from . import k2s_client
from .proxy import get_working_proxies

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".flv",
    ".wmv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".m4v",
    ".mp3",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
}

StatusCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[int, int, int, int], None]]
CaptchaCallback = k2s_client.CaptchaCallback

# Characters Windows rejects outright in a filename, plus path separators
# (a filename component must never smuggle in directory structure) and
# ASCII control characters.
_WINDOWS_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Reserved device names on Windows: illegal as a filename regardless of
# extension (e.g. "con.mp4" is just as unusable as "con").
_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_WINDOWS_RESERVED_NAMES.update(f"COM{i}" for i in range(1, 10))
_WINDOWS_RESERVED_NAMES.update(f"LPT{i}" for i in range(1, 10))


def _sanitize_filename_component(name: str) -> str:
    """Make a single filename (no directory separators) safe to use on disk.

    ``name`` may come straight from the Keep2Share API (``k2s_client.get_name``)
    or from user input, neither of which is guaranteed to be a valid Windows
    filename: it can contain characters Windows rejects outright, a reserved
    device name (``CON``, ``NUL``, ``COM1``, ...), or trailing dots/spaces
    (which Windows silently strips, so the file actually created on disk
    would not match the name callers think they wrote). Without this,
    ``Path.write_bytes`` raises a plain ``OSError`` that
    ``_mark_chunk_failed`` treats like any other chunk failure -- retried 8
    times with a misleading "IP/proxy may be blocked" error at the end.
    """
    sanitized = _WINDOWS_ILLEGAL_CHARS_RE.sub("_", name).strip().rstrip(". ")
    stem = sanitized.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        sanitized = f"_{sanitized}"
    return sanitized or "download"


# Matches a `?query=string` of meaningful length embedded in a chunk-failure
# message. `requests`/`urllib3` exceptions embed the full request URL,
# including Keep2Share's signed download URL (temp_url_sig, client_ip,
# per-user tags, ...) -- adds no diagnostic value beyond what the proxy
# address and exception type already say, and with up to 20 threads x 8
# retries floods the log with near-identical multi-hundred-character walls
# of text. Only long query strings are touched so short, meaningful ones
# (e.g. a plain "?" in prose) aren't clobbered.
_LONG_QUERY_STRING_RE = re.compile(r"\?[^\s)]{40,}")


def _truncate_error_message(message: str) -> str:
    return _LONG_QUERY_STRING_RE.sub("?<truncated>", message)


# Timeout (seconds) for the HEAD request used to discover total file size.
# Previously unset, so a blocked/unresponsive IP would hang here forever.
HEAD_REQUEST_TIMEOUT = 15

# Connect/read timeout (seconds) passed to `requests.get` for each chunk
# download. Previously a magic number inlined at the call site.
CHUNK_REQUEST_TIMEOUT = 20
# Separate from CHUNK_REQUEST_TIMEOUT: a stall watchdog checked between
# `iter_content` reads. If no new data arrives within this many seconds the
# attempt is abandoned (the size-mismatch/retry path below picks it up),
# even though the underlying socket hasn't timed out yet. Coincidentally the
# same value as CHUNK_REQUEST_TIMEOUT today, but they control different
# things and are kept as separate constants.
CHUNK_STALL_TIMEOUT = 20
# Overall deadline (seconds) for joining in-flight chunk threads when the
# scheduling loop exits. Slightly above CHUNK_REQUEST_TIMEOUT because a
# thread blocked inside `requests.get` only notices `stop_event` once the
# socket-level connect/read timeout fires; threads in the streaming loop or
# in `_acquire_proxy_lock` exit within a block read / 0.02s.
CHUNK_THREADS_JOIN_TIMEOUT = 30.0
# R2-11: minimum real-time gap (seconds) between throughput telemetry status
# messages during a download. Checked every scheduling-loop poll tick (every
# ~0.05s) but only actually emits a message once this many seconds have
# elapsed, so it doesn't flood status_callback (and, via it, the CLI/GUI log)
# with a line every tick.
TELEMETRY_REPORT_INTERVAL = 5.0


@dataclass(frozen=True)
class _DownloadContext:
    """Per-`_download_once`-invocation values shared by every chunk thread.

    Bundled into one immutable object so `_run_scheduling_loop` /
    `_download_chunk` / `_report_progress` don't each need to thread five
    separate keyword arguments through (the signatures got unwieldy when
    these were split out of the old monolithic `_download_once`).
    """

    urls: Sequence[str]
    filename: str
    headers: Dict[str, str]
    threads: int
    split_count: int
    progress_bar: tqdm
    # The following four fields exist only to let chunk threads and the
    # scheduling loop's part-reuse branch persist the resume manifest
    # (see _persist_manifest) without threading extra positional arguments
    # through _download_chunk's signature. `ranges` is the same dict object
    # `_run_scheduling_loop` iterates -- mutating a range's "downloaded" flag
    # in place is visible here too, frozen dataclass notwithstanding (frozen
    # only blocks rebinding the attribute, not mutating what it points to).
    file_id: str
    total_size: int
    bytes_per_split: int
    ranges: Dict[str, Dict[str, object]]


class FailedChunk(NamedTuple):
    """A chunk that exhausted its retry budget in ``_run_scheduling_loop``."""

    chunk_idx: str
    last_error: str
    attempt_count: int


class DownloadCancelled(RuntimeError):
    """Raised when a download is cancelled by the user."""


class ChunkDownloadFailed(RuntimeError):
    """Raised when a chunk exhausts its retry budget.

    This usually means the source IP (and/or every proxy tried) is being
    blocked or rate-limited by the host, rather than a one-off network
    hiccup. Distinguished from ``DownloadCancelled`` so callers can tell
    "the user stopped it" apart from "we gave up".
    """


# How many times a single range/chunk may fail before we give up on the
# whole download instead of retrying forever (previous behaviour: infinite
# retries with no backoff, which is what made the app appear to hang when
# every proxy/IP was blocked).
MAX_CHUNK_RETRIES = 8
# Exponential backoff between retry attempts for a single chunk.
CHUNK_RETRY_BACKOFF_BASE = 1.0
CHUNK_RETRY_BACKOFF_CAP = 30.0


def _emit_status(callback: StatusCallback, message: str) -> None:
    if callback:
        callback(message)


def parse_size(size: str) -> int:
    # NOTE: "KB"/"MB"/... here are already binary (1024-based), matching the
    # IEC units below. "KIB"/"MIB"/... previously used decimal (1000-based)
    # multipliers, which silently mis-sized any --split-size given in KiB/MiB
    # (e.g. "5MiB" parsed as 5,000,000 bytes instead of 5,242,880).
    #
    # Single-letter units (K/M/G/T) are included because the CLI's own
    # --split-size default is "20M" (see cli.py); without these aliases,
    # parse_size("20M") raised an uncaught KeyError -- not the ValueError
    # cli.py catches -- so the CLI crashed on every invocation that didn't
    # explicitly pass --split-size.
    units = {
        "B": 1,
        "K": 2**10,
        "KB": 2**10,
        "KIB": 2**10,
        "M": 2**20,
        "MB": 2**20,
        "MIB": 2**20,
        "G": 2**30,
        "GB": 2**30,
        "GIB": 2**30,
        "T": 2**40,
        "TB": 2**40,
        "TIB": 2**40,
        "": 1,
    }
    normalized = str(size).strip()
    match = re.match(r"^([\d\.]+)\s*([a-zA-Z]{0,3})$", normalized)
    if not match:
        raise ValueError(f"Invalid size value: {size}")
    number, unit = float(match.group(1)), match.group(2).upper()
    if unit not in units:
        raise ValueError(f"Invalid size value: {size}")
    return int(number * units[unit])


def human_readable_bytes(num: int) -> str:
    # Divides by 1024 at each step, so the unit labels are IEC binary
    # (KiB/MiB/...), matching gui/main_window.py's _format_speed. Previously
    # labelled "KB"/"MB"/... (the SI/decimal names) despite the binary math.
    units = ["bytes", "KiB", "MiB", "GiB", "TiB"]
    value = float(num)
    for unit in units:
        if value < 1024.0:
            return f"{value:3.3f} {unit}"
        value /= 1024.0
    return f"{value:3.3f} PiB"


class Downloader:
    def __init__(
        self,
        *,
        tmp_dir: Path | str = "tmp",
        # Debug/inspection artifact only: download() unconditionally deletes
        # this file at the start of every run and never reads it back (the
        # generated URLs expire quickly, so cross-run reuse has little
        # value) -- it exists purely so the last batch of generated
        # download URLs for a given file_id is visible on disk afterward.
        url_cache_path: Path | str = "urls.json",
        proxy_cache_path: Path | str = "proxies.txt",
        block_size: int = 32 * 1024,
        status_callback: StatusCallback = None,
        progress_callback: ProgressCallback = None,
        proxy_state_callback: Optional[Callable[[Sequence[Optional[str]], Sequence[int]], None]] = None,
        show_console_progress: bool = False,
    ) -> None:
        self.tmp_dir = Path(tmp_dir)
        self.url_cache_path = Path(url_cache_path)
        # Defaults to "proxies.txt" in the current working directory for
        # backward compatibility with get_working_proxies()'s own default;
        # pass an absolute path (e.g. a user data directory) to avoid
        # writing into whatever directory the process happens to run from.
        self.proxy_cache_path = Path(proxy_cache_path)
        self.block_size = block_size
        self.status_callback = status_callback
        self.progress_callback = progress_callback
        self.show_console_progress = show_console_progress
        self.proxy_state_callback = proxy_state_callback

        self.stop_event = threading.Event()
        self._progress_lock = threading.Lock()
        self._proxy_state_lock = threading.Lock()
        self._working_proxy_lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._bytes_downloaded = 0
        self._total_bytes = 0
        self._ranges_total = 0
        self._done_count = 0
        # R2-11 throughput telemetry: bytes actually pulled over a live
        # connection this download, split by connection type (direct vs.
        # any proxy). Deliberately excludes bytes credited via the
        # scheduling loop's part-reuse branch (resumed/already-on-disk
        # chunks never touched a live connection this run, so counting
        # them here would inflate the measured throughput).
        self._direct_bytes_downloaded = 0
        self._proxy_bytes_downloaded = 0
        self._telemetry_last_emit_time = 0.0
        self._telemetry_last_bytes = 0
        self._telemetry_last_direct_bytes = 0
        self._telemetry_last_proxy_bytes = 0

        self.proxies: List[Optional[str]] = []
        self.proxy_locks: List[threading.Lock] = []
        self.working_proxy_indexes: List[int] = []
        self.url_locks: List[threading.Lock] = []
        self._active_proxy_indexes: set[int] = set()

    @staticmethod
    def extract_file_id(url: str) -> str:
        pattern = re.compile(r"https?://(k2s.cc|keep2share.cc)/file/(.*?)(\?|/|$)")
        match = pattern.search(url)
        if not match:
            raise ValueError("Invalid URL")
        return match.group(2)

    @staticmethod
    def _resolve_filename(user_filename: Optional[str], original_name: str) -> str:
        # original_name is whatever Keep2Share's API returned for this file
        # (see k2s_client.get_name) -- untrusted input, sanitized the same
        # way user-supplied names are before it ever reaches the filesystem.
        safe_original_name = _sanitize_filename_component(original_name)
        if not user_filename:
            return safe_original_name

        # `user_filename` may legitimately carry directory components (e.g.
        # CLI `--filename out/video.mp4`); only its final path component is
        # a "filename" that needs sanitizing, and it's re-attached under the
        # original parent directory so the caller's intended output location
        # is preserved.
        user_path = Path(user_filename)
        safe_stem = _sanitize_filename_component(user_path.name)
        if not user_path.suffix:
            suffix = "".join(Path(safe_original_name).suffixes)
            if suffix:
                safe_stem = f"{safe_stem}{suffix}"
        return str(user_path.with_name(safe_stem))

    @staticmethod
    def _apply_output_dir(resolved_name: str, output_dir: Optional[Path | str]) -> str:
        # GUI-style "save to" directory: takes precedence over any directory
        # component `resolved_name` may itself carry (that's the CLI's
        # `--filename out/video.mp4` use case, which never passes
        # output_dir), so only the final path component survives.
        if output_dir is None:
            return resolved_name
        return str(Path(output_dir) / Path(resolved_name).name)

    def log(self, message: str) -> None:
        _emit_status(self.status_callback, message)

    def _part_path(self, filename: str, idx: object, split_count: int) -> Path:
        """On-disk path of one chunk's part file, always flat under ``tmp_dir``.

        Keyed off ``Path(filename).name`` rather than ``filename`` itself,
        so a ``filename`` with directory components (e.g. CLI
        ``--filename out/video.mp4``) doesn't produce a part path like
        ``tmp_dir/out/video.mp4.partNN`` -- ``tmp_dir.mkdir()`` only ever
        creates ``tmp_dir`` itself, so that nested parent would not exist
        and every chunk write would fail with the same misleading
        "possibly blocked" retry loop as an unsanitized filename.
        """
        basename = Path(filename).name
        return self.tmp_dir / f"{basename}.part{str(idx).zfill(len(str(split_count)))}"

    def _manifest_path(self, filename: str) -> Path:
        """On-disk path of the resume manifest, flat under ``tmp_dir`` like
        ``_part_path`` (same reasoning: keyed off the basename only)."""
        return self.tmp_dir / f"{Path(filename).name}.manifest.json"

    def _load_manifest(self, filename: str) -> Optional[dict]:
        """Best-effort read of a previous run's manifest for ``filename``.

        Returns ``None`` on anything short of a fully well-formed JSON
        object -- a missing, partially-written, or corrupt manifest is
        treated exactly like "no previous progress", never as an error that
        should abort the (otherwise perfectly startable) download.
        """
        manifest_path = self._manifest_path(filename)
        if not manifest_path.exists():
            return None
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _persist_manifest(self, ctx: _DownloadContext) -> None:
        """Write (or refresh) the resume manifest for the current download.

        This is what makes the download's progress visible on disk (the
        user's original complaint: no growing files, no record of whether
        anything was actually downloaded) and lets a *later* run tell "my
        own leftover part files" apart from a stale/foreign set that just
        happens to share this filename -- see ``_prepare_resume``. Written
        to a temp file and ``replace()``d into place (atomic overwrite, and
        Windows-safe -- plain ``rename`` refuses to overwrite an existing
        destination on Windows).

        Best-effort: a write failure here must never abort or fail an
        otherwise-successful chunk, so it only logs and moves on.
        """
        manifest_path = self._manifest_path(ctx.filename)
        # Snapshot every range's flags under _progress_lock -- the same lock
        # _download_chunk's completion path and the scheduling loop's
        # part-reuse branch hold while flipping "downloaded" -- so this
        # dict comprehension can't observe some ranges mid-update and
        # others not (in-memory only, no I/O happens while the lock is
        # held).
        with self._progress_lock:
            ranges_snapshot = {
                idx: {
                    "range": meta["range"],
                    "bytes": meta["bytes"],
                    "downloaded": bool(meta.get("downloaded")),
                }
                for idx, meta in ctx.ranges.items()
            }
        payload = {
            "file_id": ctx.file_id,
            "total_size": ctx.total_size,
            "split_size": ctx.bytes_per_split,
            "split_count": len(ctx.ranges),
            "ranges": ranges_snapshot,
            "updated_at": time.time(),
        }
        tmp_manifest_path = manifest_path.with_name(manifest_path.name + ".tmp")
        try:
            with self._manifest_lock:
                with tmp_manifest_path.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                tmp_manifest_path.replace(manifest_path)
        except OSError as exc:
            self.log(f"Could not update resume manifest ({exc}); download continues without it.")

    def _prepare_resume(
        self,
        filename: str,
        file_id: str,
        total_size: int,
        bytes_per_split: int,
        ranges: Dict[str, Dict[str, object]],
    ) -> None:
        """Detect and validate resumable progress before the scheduling loop starts.

        A manifest only authorizes resume when its ``file_id``, ``total_size``,
        and range layout all match this run -- otherwise leftover part files
        under this filename belong to an unrelated previous download (e.g. a
        different Keep2Share link that happened to resolve to the same
        filename) and reusing them by byte-count coincidence alone (the
        scheduling loop's part-reuse branch has no other way to tell) would
        silently splice foreign bytes into this file. When the manifest is
        absent or doesn't match, any stray ``.partNN``/manifest files for
        this filename are cleared instead of trusted.

        For a range the manifest *does* vouch for, the part file on disk is
        still independently re-verified (existence + byte count) rather than
        trusted blindly -- the manifest records intent, the file on disk is
        the source of truth.
        """
        manifest = self._load_manifest(filename)
        manifest_matches = (
            manifest is not None
            and manifest.get("file_id") == file_id
            and manifest.get("total_size") == total_size
            and manifest.get("split_size") == bytes_per_split
            and manifest.get("split_count") == len(ranges)
        )

        if not manifest_matches:
            removed_any = self._clear_stale_part_files(filename)
            if removed_any:
                self.log(
                    "Found leftover temporary files for this filename from an "
                    "unrelated or incompatible previous download; clearing them "
                    "before starting fresh."
                )
            self.log("No previous progress found; starting a fresh download.")
            return

        manifest_ranges = manifest.get("ranges", {})
        resumed_count = 0
        resumed_bytes = 0
        for idx, meta in ranges.items():
            if not manifest_ranges.get(idx, {}).get("downloaded"):
                continue
            part_path = self._part_path(filename, idx, len(ranges))
            if not part_path.exists():
                continue
            if not math.isclose(part_path.stat().st_size, meta["bytes"], abs_tol=1):
                continue
            meta["downloaded"] = True
            resumed_count += 1
            resumed_bytes += int(meta["bytes"])

        if resumed_count:
            self.log(
                f"Resuming: found {resumed_count}/{len(ranges)} segment(s) already "
                f"downloaded ({human_readable_bytes(resumed_bytes)}); continuing."
            )
            self._bytes_downloaded += resumed_bytes
            self._done_count += resumed_count
        else:
            self.log("No previous progress found; starting a fresh download.")

    def _clear_stale_part_files(self, filename: str) -> bool:
        """Remove any part/manifest files for ``filename`` that a mismatched
        or missing manifest means we can no longer trust. Returns whether
        anything was actually removed (purely for logging)."""
        if not self.tmp_dir.exists():
            return False
        basename = Path(filename).name
        # Match only this app's own part-file naming (".partN" / ".partN.tmp",
        # any digit width -- a stale set from a different split layout can
        # be padded to a different width than the current run's) rather
        # than a bare "*" glob, since this loop deletes what it matches and
        # a wildcard suffix would also sweep up anything else that merely
        # starts with the same prefix.
        part_name_pattern = re.compile(rf"^{re.escape(basename)}\.part\d+(?:\.tmp)?$")
        removed = False
        for stale_path in self.tmp_dir.iterdir():
            if not part_name_pattern.match(stale_path.name):
                continue
            try:
                stale_path.unlink()
                removed = True
            except OSError:
                pass
        manifest_path = self._manifest_path(filename)
        if manifest_path.exists():
            try:
                manifest_path.unlink()
                removed = True
            except OSError:
                pass
        return removed

    def _mark_chunk_failed(self, range_meta: Dict[str, object], reason: str) -> None:
        """Record a failed chunk attempt with a bounded retry budget.

        Increments ``range_meta["attempts"]`` and schedules the next retry
        with exponential backoff. Once ``MAX_CHUNK_RETRIES`` is exceeded,
        marks the range as permanently ``failed`` so the scheduling loop in
        ``_download_once`` can stop and raise ``ChunkDownloadFailed`` instead
        of retrying forever.
        """
        reason = _truncate_error_message(reason)
        attempts = int(range_meta.get("attempts", 0)) + 1
        range_meta["attempts"] = attempts
        range_meta["last_error"] = reason
        range_meta["inUse"] = False

        if attempts >= MAX_CHUNK_RETRIES:
            range_meta["failed"] = True
            self.log(f"Chunk permanently failed after {attempts} attempts ({reason}).")
            return

        backoff = min(CHUNK_RETRY_BACKOFF_CAP, CHUNK_RETRY_BACKOFF_BASE * (2 ** (attempts - 1)))
        range_meta["next_retry_at"] = time.time() + backoff
        self.log(
            f"Chunk attempt {attempts}/{MAX_CHUNK_RETRIES} failed ({reason}); "
            f"retrying in {backoff:.1f}s."
        )

    def _acquire_proxy_lock(self) -> Optional[int]:
        """Pick a proxy and atomically acquire its lock.

        Previously this was a check-then-act: scan for a lock that reports
        ``locked() == False``, then separately call blocking ``acquire()``.
        Two threads could both observe the same lock as free and race for
        it; the loser would then block indefinitely inside a blocking
        ``acquire()`` while still holding its ``url_locks`` slot, and the
        list of "known working" proxy indexes was read/appended from
        multiple threads with no lock at all. This instead always attempts
        a non-blocking acquire and only proceeds once one actually
        succeeds, and reads ``working_proxy_indexes`` under a dedicated
        lock. Returns ``None`` if ``stop_event`` is set while waiting.
        """
        while not self.stop_event.is_set():
            # Prefer the direct connection (index 0, always None -- see
            # proxy.get_working_proxies) over routing through a third-party
            # proxy. The proxy list is sourced from a public, unauthenticated
            # scraper and carried over plain HTTP even for an HTTPS target
            # (see the ``prox`` dict below), so it can be MITM'd; only fall
            # back to it when the direct slot is already busy or unusable.
            if self.proxies and self.proxies[0] is None and self.proxy_locks[0].acquire(blocking=False):
                return 0

            with self._working_proxy_lock:
                known = list(self.working_proxy_indexes)
            for candidate in known:
                if self.proxy_locks[candidate].acquire(blocking=False):
                    return candidate

            candidate = random.randint(0, len(self.proxies) - 1)
            if self.proxy_locks[candidate].acquire(blocking=False):
                return candidate

            time.sleep(0.02)
        return None

    def refresh_proxies(self, *, refresh: bool = False) -> None:
        self.proxies = get_working_proxies(
            refresh=refresh,
            status_callback=self.status_callback,
            cache_path=self.proxy_cache_path,
        )
        self.proxy_locks = [threading.Lock() for _ in self.proxies]
        with self._working_proxy_lock:
            self.working_proxy_indexes = []
        self._active_proxy_indexes = set()
        self._notify_proxy_state()

    def should_check_media(self, filename: str) -> bool:
        return Path(filename).suffix.lower() in MEDIA_EXTENSIONS

    def _notify_proxy_state(self) -> None:
        if self.proxy_state_callback:
            with self._proxy_state_lock:
                proxies = list(self.proxies)
                active = sorted(self._active_proxy_indexes)
            self.proxy_state_callback(proxies, active)

    def cancel(self) -> None:
        self.stop_event.set()

    def download(
        self,
        url: str,
        *,
        filename: Optional[str] = None,
        output_dir: Optional[Path | str] = None,
        threads: int = 20,
        split_size: int = 20 * 1024 * 1024,
        captcha_callback: Optional[CaptchaCallback] = None,
        ensure_media_check: bool = True,
    ) -> Path:

        if split_size < 5 * 1024 * 1024:
            raise ValueError("Split size must be at least 5M")

        # Clear the stop flag up front so a cancel issued at any later point
        # (including during URL generation) is never wiped out.
        self.stop_event.clear()

        if not self.proxies:
            self.refresh_proxies()

        file_id = self.extract_file_id(url)
        try:
            original_name = k2s_client.get_name(file_id)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"Could not fetch file info from Keep2Share (network unreachable or IP blocked): {exc}"
            ) from exc
        resolved_name = self._apply_output_dir(self._resolve_filename(filename, original_name), output_dir)

        urls = []

        if self.url_cache_path.exists():
            try:
                self.url_cache_path.unlink()
            except OSError:
                pass

        try:
            urls = k2s_client.generate_download_urls(
                file_id,
                count=threads,
                proxies=self.proxies,
                captcha_callback=captcha_callback,
                status_callback=self.status_callback,
                stop_event=self.stop_event,
            )
        except k2s_client.OperationCancelled as exc:
            raise DownloadCancelled(str(exc)) from exc
        self._cache_urls(file_id, urls)

        if len(urls) < threads:
            self.log(
                f"Only {len(urls)} download URLs available; reducing connections from {threads} to {len(urls)}."
            )
            threads = len(urls)

        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        redownloaded = False
        current_split = split_size

        while True:
            result = self._download_once(urls, resolved_name, threads, current_split, file_id=file_id)
            if self.stop_event.is_set():
                raise DownloadCancelled("Download cancelled")

            if ensure_media_check and self.should_check_media(resolved_name) and which("ffmpeg"):
                if not self._check_media(Path(resolved_name)):
                    if not redownloaded:
                        self.log("Video appears corrupted. Retrying with a larger chunk size ...")
                        redownloaded = True
                        current_split *= 2
                        continue
                    self.log("Video is still corrupted after retry.")
            break

        return result

    def _cache_urls(self, file_id: str, urls: Sequence[str]) -> None:
        if self.url_cache_path.exists():
            try:
                with self.url_cache_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                data = {}
        else:
            data = {}

        data[file_id] = list(urls)
        with self.url_cache_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)

    def _fetch_total_size(self, url: str, headers: Dict[str, str]) -> int:
        try:
            head_response = requests.head(
                url,
                allow_redirects=True,
                headers=headers,
                timeout=HEAD_REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"Could not reach the download host to determine file size (possibly blocked): {exc}"
            ) from exc

        if not head_response.ok:
            # A non-2xx status here (403/429/5xx, ...) usually means this
            # download URL has expired or is blocked -- the Content-Length
            # on an error page describes that error page, not the actual
            # file. Reading it anyway would build every chunk's byte range
            # from the wrong total size, so every chunk would then fail
            # with a "size mismatch" that doesn't point at the real cause,
            # burning a full retry cycle before the download gives up.
            raise RuntimeError(
                f"Could not determine file size: HEAD request returned "
                f"HTTP {head_response.status_code}. The download URL may have "
                "expired or be blocked."
            )

        size_in_bytes = head_response.headers.get("Content-Length")
        if not size_in_bytes:
            raise RuntimeError("Size cannot be determined.")

        total_size = int(size_in_bytes)
        if total_size < 0:
            total_size = total_size + 2**32
        return total_size

    def _report_progress(
        self, delta: int, ctx: _DownloadContext, *, is_direct: Optional[bool] = None
    ) -> None:
        if delta == 0:
            return
        with self._progress_lock:
            self._bytes_downloaded += delta
            downloaded = self._bytes_downloaded
            done = self._done_count
            # is_direct is None for bytes credited without a live connection
            # this run (the scheduling loop's part-reuse branch crediting an
            # already-on-disk part) -- those must not count toward
            # direct/proxy throughput telemetry, which only means to measure
            # this run's actual network activity.
            if is_direct is True:
                self._direct_bytes_downloaded += delta
            elif is_direct is False:
                self._proxy_bytes_downloaded += delta
        if self.progress_callback:
            self.progress_callback(downloaded, self._total_bytes, done, self._ranges_total)
        if not ctx.progress_bar.disable:
            ctx.progress_bar.update(delta)

    def _maybe_report_throughput(self) -> None:
        """R2-11: periodically log aggregate throughput and its direct-vs-proxy
        split, so real usage can validate (or refute) the working hypothesis
        that this project's speed limit is per-connection rather than
        per-IP (see docs/ai/todolist.md R2-11) -- without this, there is no
        way to observe that split short of external packet capture.
        """
        now = time.time()
        with self._progress_lock:
            elapsed = now - self._telemetry_last_emit_time
            if elapsed < TELEMETRY_REPORT_INTERVAL:
                return
            interval_bytes = self._bytes_downloaded - self._telemetry_last_bytes
            interval_direct = self._direct_bytes_downloaded - self._telemetry_last_direct_bytes
            interval_proxy = self._proxy_bytes_downloaded - self._telemetry_last_proxy_bytes
            self._telemetry_last_emit_time = now
            self._telemetry_last_bytes = self._bytes_downloaded
            self._telemetry_last_direct_bytes = self._direct_bytes_downloaded
            self._telemetry_last_proxy_bytes = self._proxy_bytes_downloaded

        if interval_bytes <= 0:
            return

        live_bytes = interval_direct + interval_proxy
        if live_bytes > 0:
            direct_pct = 100.0 * interval_direct / live_bytes
            proxy_pct = 100.0 * interval_proxy / live_bytes
            split_text = f"direct: {direct_pct:.0f}%, proxy: {proxy_pct:.0f}%"
        else:
            # Every byte this interval came from the part-reuse branch
            # (resumed chunks), not a live connection -- nothing to split.
            split_text = "no live connection activity (resumed chunks only)"

        speed = interval_bytes / elapsed
        active = len(self._active_proxy_indexes)
        self.log(
            f"Throughput: {human_readable_bytes(speed)}/s over the last {elapsed:.0f}s "
            f"({split_text}; {active} active connection(s))."
        )

    def _download_chunk(
        self,
        index: str,
        range_meta: Dict[str, object],
        thread_index: int,
        ctx: _DownloadContext,
    ) -> None:
        """Download a single byte-range chunk in its own thread.

        Cancellation is communicated purely via ``self.stop_event`` -- there
        used to also be a ``nonlocal stop`` flag mirrored from here into the
        scheduling loop (now ``_run_scheduling_loop``), but every path that
        set it did so only when ``self.stop_event`` was already set (or being
        set in the same statement), making it entirely redundant with -- and
        a source of unnecessary shared mutable state across threads compared
        to -- checking ``self.stop_event`` directly.
        """
        if self.stop_event.is_set():
            return

        chunk_range = range_meta["range"]  # type: ignore[index]
        expected_bytes = int(range_meta["bytes"])  # type: ignore[arg-type]
        tmp_filename = self._part_path(ctx.filename, index, ctx.split_count)
        # Downloaded to this in-progress name first, atomically renamed to
        # tmp_filename only once the full expected byte count is confirmed
        # on disk. Two things this buys over the old "buffer the whole
        # chunk in RAM, write it out in one shot at the end" approach: (1)
        # an in-progress chunk is now visible growing on disk instead of
        # invisible until 100% done (R2-7/R2-13's original motivation --
        # users had no way to tell whether anything was happening at all),
        # and (2) the atomic rename means the scheduling loop's part-reuse
        # branch (which only ever looks at tmp_filename, never *.tmp) can
        # never mistake a half-written attempt for a complete one.
        tmp_write_path = tmp_filename.with_name(tmp_filename.name + ".tmp")
        bytes_written = 0

        chunk_start_time = time.time()

        added_to_active = False
        proxy_idx_or_none = self._acquire_proxy_lock()
        if proxy_idx_or_none is None:
            return
        proxy_idx: int = proxy_idx_or_none

        proxy_value = self.proxies[proxy_idx]
        # NOTE: the proxy connection itself is unauthenticated plain
        # HTTP even though the target URL is HTTPS -- requests tunnels
        # HTTPS through an HTTP CONNECT to this proxy, so the proxy
        # operator sits in a position to observe/tamper with traffic.
        # Combined with proxies.txt being sourced from a public,
        # unauthenticated scraper (see proxy.py), do not rely on this
        # path for sensitive downloads. _acquire_proxy_lock() already
        # prefers the direct connection (index 0) over any proxy.
        prox = {"https": f"http://{proxy_value}"} if proxy_value else None
        added_to_active = True
        with self._proxy_state_lock:
            self._active_proxy_indexes.add(proxy_idx)
        self._notify_proxy_state()

        try:
            try:
                response = requests.get(
                    ctx.urls[thread_index],
                    headers={"Range": f"bytes={chunk_range}", "User-Agent": ctx.headers["User-Agent"]},
                    stream=True,
                    proxies=prox,
                    timeout=CHUNK_REQUEST_TIMEOUT,
                )

                if response.status_code not in (200, 206):
                    # A non-success status (403/429/5xx, etc.) usually means
                    # this proxy or our own IP has been blocked/rate-limited
                    # by the host. Previously this was ignored and only
                    # caught later by a coincidental byte-count mismatch;
                    # detecting it here lets the range be retried sooner
                    # and lets us record *why* it failed for the caller.
                    response.close()
                    self._mark_chunk_failed(
                        range_meta,
                        f"HTTP {response.status_code} via proxy {proxy_value or 'LOCAL'}",
                    )
                    return

                with tmp_write_path.open("wb") as part_file:
                    for data in response.iter_content(self.block_size):
                        if self.stop_event.is_set():
                            break
                        if chunk_start_time + CHUNK_STALL_TIMEOUT < time.time():
                            break
                        chunk_start_time = time.time()
                        part_file.write(data)
                        # Without this, the write sits in Python's internal
                        # buffer and the .tmp file can appear empty/stale to
                        # anything else looking at it (a user checking the
                        # temp folder, another process) until the buffer
                        # happens to fill or the file is closed -- defeating
                        # the whole point of streaming to disk incrementally.
                        part_file.flush()
                        bytes_written += len(data)
                        self._report_progress(len(data), ctx, is_direct=proxy_idx == 0)
            except requests.exceptions.RequestException as exc:
                # Network-level failure (connection error, read timeout,
                # a chunked-encoding error mid-stream, etc.). Previously
                # a bare ``contextlib.suppress(Exception)`` swallowed
                # this entirely, so the only visible symptom was a
                # coincidental "size mismatch" below with no indication
                # of *why* the request actually failed.
                tmp_write_path.unlink(missing_ok=True)
                self._mark_chunk_failed(
                    range_meta,
                    f"request error via proxy {proxy_value or 'LOCAL'}: {exc}",
                )
                return

            if not math.isclose(bytes_written, expected_bytes, abs_tol=1):
                self._report_progress(-bytes_written, ctx, is_direct=proxy_idx == 0)
                tmp_write_path.unlink(missing_ok=True)
                self._mark_chunk_failed(
                    range_meta, f"size mismatch: got {bytes_written} expected {expected_bytes}"
                )
                # Do NOT release url_locks[thread_index] here: the ``finally``
                # below is the single release point. Releasing early meant the
                # scheduler could hand this slot to a new chunk, and the
                # (unowned) second release in ``finally`` would then free the
                # lock out from under that new holder, allowing two chunks to
                # share one download URL concurrently.
                return

            range_meta.pop("last_error", None)
            tmp_write_path.replace(tmp_filename)
            with self._working_proxy_lock:
                is_newly_known = proxy_idx not in self.working_proxy_indexes
                if is_newly_known:
                    self.working_proxy_indexes.append(proxy_idx)
            if is_newly_known:
                self._notify_proxy_state()

            # Publish completion atomically, and with ``downloaded`` set
            # before ``inUse`` is cleared. The scheduling loop reads
            # ``inUse`` then ``downloaded``; clearing ``inUse`` first opened
            # a window where the part-file-reuse branch saw a finished part
            # with both flags False and counted this range a second time
            # (progress > 100%, premature exit from the scheduling loop, and
            # an uncategorized FileNotFoundError from _merge_parts).
            with self._progress_lock:
                range_meta["downloaded"] = True
                range_meta["inUse"] = False
                self._done_count += 1
                done = self._done_count
            self._persist_manifest(ctx)
            if not ctx.progress_bar.disable:
                ctx.progress_bar.desc = f"[{done}/{self._ranges_total}] Downloaded"
            if self.progress_callback:
                self.progress_callback(self._bytes_downloaded, self._total_bytes, done, self._ranges_total)
        except Exception as exc:  # noqa: BLE001 - a chunk thread must never crash
            # silently and leave range_meta["inUse"] stuck at True forever
            # (the scheduling loop would then wait on this range forever).
            # Anything unexpected past the request/streaming stage (e.g. a
            # disk write failure) is recorded the same way a network
            # failure is, instead of the old blanket suppress-and-ignore.
            tmp_write_path.unlink(missing_ok=True)
            self._mark_chunk_failed(
                range_meta, f"unexpected error via proxy {proxy_value or 'LOCAL'}: {exc}"
            )
        finally:
            if added_to_active:
                with self._proxy_state_lock:
                    self._active_proxy_indexes.discard(proxy_idx)
                self._notify_proxy_state()
            if self.url_locks[thread_index].locked():
                self.url_locks[thread_index].release()
            # proxy_idx is an int here: _acquire_proxy_lock() returning None
            # returns out of the function before this try/finally is entered.
            if self.proxy_locks[proxy_idx].locked():
                self.proxy_locks[proxy_idx].release()

    def _run_scheduling_loop(
        self,
        ranges: Dict[str, Dict[str, object]],
        ctx: _DownloadContext,
    ) -> Optional[FailedChunk]:
        """Dispatch a download thread for each pending range until all are
        done, cancelled, or one has permanently failed.

        Returns a ``FailedChunk`` if a range exhausted its retry budget, or
        ``None`` otherwise (both on full success and on cancellation -- the
        caller distinguishes the latter by checking ``self.stop_event``
        afterward).
        """
        failed_chunk: Optional[FailedChunk] = None
        stop_scheduling = False
        chunk_threads: List[threading.Thread] = []

        try:
            while self._done_count < len(ranges):
                if stop_scheduling:
                    break
                chunk_threads = [t for t in chunk_threads if t.is_alive()]
                now = time.time()
                for idx, meta in ranges.items():
                    if self.stop_event.is_set():
                        stop_scheduling = True
                        break
                    if meta.get("failed"):
                        # _mark_chunk_failed always sets meta["attempts"] to
                        # an int before "failed" is ever set, so int() below
                        # is a plain (redundant at runtime) conversion, not a
                        # type-unsafe assumption.
                        failed_chunk = FailedChunk(
                            chunk_idx=idx,
                            last_error=str(meta.get("last_error", "unknown error")),
                            attempt_count=int(meta["attempts"]),
                        )
                        stop_scheduling = True
                        break
                    if meta["inUse"] or meta["downloaded"]:
                        continue
                    if meta.get("next_retry_at", 0) > now:
                        # Still within this chunk's backoff window; skip it
                        # for now instead of hammering the same failing
                        # source again immediately.
                        continue

                    part_path = self._part_path(ctx.filename, idx, ctx.split_count)
                    if part_path.exists():
                        # stat() only, not read_bytes() -- this branch fires
                        # once per pending range per poll tick, and reading
                        # a whole (potentially 20+MiB) part file into memory
                        # just to check its length is exactly the kind of
                        # needless buffering R2-7 removed from the download
                        # path itself.
                        existing_size = part_path.stat().st_size
                        if math.isclose(existing_size, meta["bytes"], abs_tol=1):
                            # Claim the range under _progress_lock so this
                            # check-and-mark cannot interleave with the chunk
                            # thread's own completion publish (which holds the
                            # same lock) and double-count the range.
                            with self._progress_lock:
                                claimed = not meta["downloaded"]
                                if claimed:
                                    meta["downloaded"] = True
                                    self._done_count += 1
                                    done = self._done_count
                            if claimed:
                                self._report_progress(int(meta["bytes"]), ctx)
                                self._persist_manifest(ctx)
                                if not ctx.progress_bar.disable:
                                    ctx.progress_bar.desc = f"[{done}/{len(ranges)}] Downloaded"
                            # Either way the part is complete on disk; never
                            # fall through and dispatch it again.
                            continue
                        else:
                            part_path.unlink()

                    for thread_index in range(ctx.threads):
                        if self.url_locks[thread_index].locked():
                            continue
                        self.url_locks[thread_index].acquire()
                        meta["inUse"] = True
                        chunk_thread = threading.Thread(
                            target=self._download_chunk,
                            args=(idx, meta, thread_index, ctx),
                            daemon=True,
                        )
                        chunk_threads.append(chunk_thread)
                        chunk_thread.start()
                        break
                self._maybe_report_throughput()
                time.sleep(0.05)
        except KeyboardInterrupt:
            self.cancel()
        finally:
            if failed_chunk is not None:
                # A permanently-failed range aborts the whole download, so
                # signal the in-flight chunk threads too; otherwise the join
                # below would wait out full chunk transfers. download()
                # clears stop_event at the start of every run, and
                # _download_once raises ChunkDownloadFailed before it ever
                # consults stop_event, so this cannot be mistaken for a
                # user cancellation.
                self.stop_event.set()
            # Wait for in-flight chunk threads before touching their locks.
            # Releasing url/proxy locks while a thread still held them let
            # the next download round (e.g. an immediate GUI retry) write to
            # the same tmp part files concurrently and corrupt them.
            join_deadline = time.time() + CHUNK_THREADS_JOIN_TIMEOUT
            for chunk_thread in chunk_threads:
                chunk_thread.join(timeout=max(0.0, join_deadline - time.time()))
            leftover = sum(1 for t in chunk_threads if t.is_alive())
            if leftover:
                self.log(
                    f"{leftover} chunk thread(s) still running after "
                    f"{CHUNK_THREADS_JOIN_TIMEOUT:.0f}s; releasing their locks anyway."
                )
            for lock in self.url_locks:
                if lock.locked():
                    lock.release()
            for lock in self.proxy_locks:
                if lock.locked():
                    lock.release()
            self._notify_proxy_state()

        return failed_chunk

    def _merge_parts(self, ranges: Dict[str, Dict[str, object]], filename: str) -> Path:
        target_path = Path(filename)
        # filename may carry directory components the caller expects to be
        # created for them (e.g. CLI --filename out/video.mp4); only
        # tmp_dir is guaranteed to exist at this point.
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()

        split_count = len(ranges)
        with target_path.open("wb") as handle:
            for idx in range(split_count):
                part_path = self._part_path(filename, idx, split_count)
                with part_path.open("rb") as chunk:
                    # copyfileobj streams in fixed-size blocks rather than
                    # chunk.read()'s old one-shot whole-file read, so merging
                    # doesn't hold a second full part in memory on top of
                    # whatever's already buffered for the output handle.
                    copyfileobj(chunk, handle)
                part_path.unlink()

        # Resume manifest's job ends at a successful merge -- every part it
        # was tracking has just been consumed above, so nothing is left to
        # resume and keeping it around would only wrongly vouch for a
        # *future* unrelated download that happens to reuse this filename.
        self._manifest_path(filename).unlink(missing_ok=True)

        self.log(f"Finished writing {filename}")
        self.log(f"File Size: {human_readable_bytes(target_path.stat().st_size)}")
        return target_path

    def _download_once(
        self,
        urls: Sequence[str],
        filename: str,
        threads: int,
        bytes_per_split: int,
        file_id: str = "",
    ) -> Path:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36"
        }
        total_size = self._fetch_total_size(urls[-1], headers)
        self._total_bytes = total_size
        self._bytes_downloaded = 0
        self._done_count = 0
        self._direct_bytes_downloaded = 0
        self._proxy_bytes_downloaded = 0
        self._telemetry_last_emit_time = time.time()
        self._telemetry_last_bytes = 0
        self._telemetry_last_direct_bytes = 0
        self._telemetry_last_proxy_bytes = 0

        split_count = max(1, math.ceil(total_size / bytes_per_split))
        ranges = self._build_ranges(total_size, split_count)
        self._ranges_total = len(ranges)
        self.url_locks = [threading.Lock() for _ in range(threads)]

        # Must run before dispatching any chunk: it decides, per range,
        # whether an on-disk part file from a previous attempt is safe to
        # keep (matching manifest) or must be cleared as stale/foreign
        # (mismatched or absent manifest) -- see _prepare_resume's
        # docstring. This also updates self._bytes_downloaded/_done_count
        # for whatever it finds already complete.
        self._prepare_resume(filename, file_id, total_size, bytes_per_split, ranges)

        ctx = _DownloadContext(
            urls=urls,
            filename=filename,
            headers=headers,
            threads=threads,
            split_count=split_count,
            progress_bar=tqdm(
                desc=f"[0/{len(ranges)}] Downloaded",
                total=total_size,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
                disable=not self.show_console_progress,
            ),
            file_id=file_id,
            total_size=total_size,
            bytes_per_split=bytes_per_split,
            ranges=ranges,
        )
        if self._done_count:
            ctx.progress_bar.update(self._bytes_downloaded)
            ctx.progress_bar.desc = f"[{self._done_count}/{len(ranges)}] Downloaded"
        self._persist_manifest(ctx)

        try:
            failed_chunk = self._run_scheduling_loop(ranges, ctx)
        finally:
            ctx.progress_bar.close()

        if failed_chunk is not None:
            raise ChunkDownloadFailed(
                f"Chunk {failed_chunk.chunk_idx} failed after {failed_chunk.attempt_count} attempts "
                f"(last error: {failed_chunk.last_error}). The source IP and/or every proxy tried may "
                "be blocked or rate-limited. If you're on a dynamic IP, restarting your router/modem "
                "to get a new one may help."
            )

        if self.stop_event.is_set():
            raise DownloadCancelled("Download cancelled")

        return self._merge_parts(ranges, filename)

    @staticmethod
    def _build_ranges(total_value: int, split_count: int) -> Dict[str, Dict[str, object]]:
        """Split ``[0, total_value)`` into ``split_count`` contiguous byte ranges.

        Each boundary is derived from the previous range's end (cumulative),
        not recomputed independently per range. The old implementation
        rounded ``start``/``end`` separately for every range, which could
        leave a 1-byte gap or overlap between adjacent ranges depending on
        ``total_value``/``split_count`` -- silently corrupting the
        reassembled file, since a gap byte is never downloaded and an
        overlap byte gets written twice.
        """
        range_dict: Dict[str, Dict[str, object]] = {}
        start = 0
        for i in range(split_count):
            if i == split_count - 1:
                end = total_value - 1
            else:
                end = int(round((i + 1) * total_value / split_count)) - 1
            range_dict[str(i)] = {
                "inUse": False,
                "downloaded": False,
                "range": f"{start}-{end}",
                "bytes": end - start + 1,
            }
            start = end + 1
        return range_dict

    @staticmethod
    def _check_media(video_path: Path) -> bool:
        command = [
            "ffmpeg",
            "-i",
            str(video_path),
            "-c",
            "copy",
            "-f",
            "null",
            os.devnull,
            "-v",
            "warning",
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return result.returncode == 0 and not result.stdout
