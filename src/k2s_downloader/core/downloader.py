from __future__ import annotations

import io
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
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import requests
from shutil import which
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
        self._bytes_downloaded = 0
        self._total_bytes = 0
        self._ranges_total = 0
        self._done_count = 0

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
        if not user_filename:
            return original_name
        path = Path(user_filename)
        if path.suffix:
            return user_filename
        suffix = "".join(Path(original_name).suffixes)
        return f"{user_filename}{suffix}" if suffix else user_filename

    def log(self, message: str) -> None:
        _emit_status(self.status_callback, message)

    def _mark_chunk_failed(self, range_meta: Dict[str, object], reason: str) -> None:
        """Record a failed chunk attempt with a bounded retry budget.

        Increments ``range_meta["attempts"]`` and schedules the next retry
        with exponential backoff. Once ``MAX_CHUNK_RETRIES`` is exceeded,
        marks the range as permanently ``failed`` so the scheduling loop in
        ``_download_once`` can stop and raise ``ChunkDownloadFailed`` instead
        of retrying forever.
        """
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
        resolved_name = self._resolve_filename(filename, original_name)

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
            result = self._download_once(urls, resolved_name, threads, current_split)
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

    def _load_cached_urls(self, file_id: str) -> List[str]:
        if not self.url_cache_path.exists():
            return []
        try:
            with self.url_cache_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data.get(file_id, [])
        except Exception:
            return []

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

        size_in_bytes = head_response.headers.get("Content-Length")
        if not size_in_bytes:
            raise RuntimeError("Size cannot be determined.")

        total_size = int(size_in_bytes)
        if total_size < 0:
            total_size = total_size + 2**32
        return total_size

    def _report_progress(self, delta: int, ctx: _DownloadContext) -> None:
        if delta == 0:
            return
        with self._progress_lock:
            self._bytes_downloaded += delta
            downloaded = self._bytes_downloaded
            done = self._done_count
        if self.progress_callback:
            self.progress_callback(downloaded, self._total_bytes, done, self._ranges_total)
        if not ctx.progress_bar.disable:
            ctx.progress_bar.update(delta)

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
        tmp_filename = self.tmp_dir / f"{ctx.filename}.part{str(index).zfill(len(str(ctx.split_count)))}"

        chunk_start_time = time.time()
        buffer = io.BytesIO()

        added_to_active = False
        proxy_idx = self._acquire_proxy_lock()
        if proxy_idx is None:
            return

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

                for data in response.iter_content(self.block_size):
                    if self.stop_event.is_set():
                        break
                    if chunk_start_time + CHUNK_STALL_TIMEOUT < time.time():
                        break
                    chunk_start_time = time.time()
                    buffer.write(data)
                    self._report_progress(len(data), ctx)
            except requests.exceptions.RequestException as exc:
                # Network-level failure (connection error, read timeout,
                # a chunked-encoding error mid-stream, etc.). Previously
                # a bare ``contextlib.suppress(Exception)`` swallowed
                # this entirely, so the only visible symptom was a
                # coincidental "size mismatch" below with no indication
                # of *why* the request actually failed.
                self._mark_chunk_failed(
                    range_meta,
                    f"request error via proxy {proxy_value or 'LOCAL'}: {exc}",
                )
                return

            chunk_bytes = len(buffer.getvalue())
            if not math.isclose(chunk_bytes, expected_bytes, abs_tol=1):
                self._report_progress(-chunk_bytes, ctx)
                self._mark_chunk_failed(
                    range_meta, f"size mismatch: got {chunk_bytes} expected {expected_bytes}"
                )
                if self.url_locks[thread_index].locked():
                    self.url_locks[thread_index].release()
                return

            range_meta.pop("last_error", None)
            tmp_filename.write_bytes(buffer.getvalue())
            with self._working_proxy_lock:
                is_newly_known = proxy_idx not in self.working_proxy_indexes
                if is_newly_known:
                    self.working_proxy_indexes.append(proxy_idx)
            if is_newly_known:
                self._notify_proxy_state()

            range_meta["inUse"] = False
            range_meta["downloaded"] = True

            with self._progress_lock:
                self._done_count += 1
                done = self._done_count
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
            if self.proxy_locks[proxy_idx].locked():
                self.proxy_locks[proxy_idx].release()

    def _run_scheduling_loop(
        self,
        ranges: Dict[str, Dict[str, object]],
        ctx: _DownloadContext,
    ) -> Optional[Tuple[str, str, int]]:
        """Dispatch a download thread for each pending range until all are
        done, cancelled, or one has permanently failed.

        Returns a ``(chunk_idx, last_error_reason, attempt_count)`` tuple if
        a range exhausted its retry budget, or ``None`` otherwise (both on
        full success and on cancellation -- the caller distinguishes the
        latter by checking ``self.stop_event`` afterward).
        """
        failed_chunk: Optional[Tuple[str, str, int]] = None
        stop_scheduling = False

        try:
            while self._done_count < len(ranges):
                if stop_scheduling:
                    break
                now = time.time()
                for idx, meta in ranges.items():
                    if self.stop_event.is_set():
                        stop_scheduling = True
                        break
                    if meta.get("failed"):
                        failed_chunk = (
                            idx,
                            str(meta.get("last_error", "unknown error")),
                            int(meta.get("attempts", 0)),  # type: ignore[arg-type]
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

                    part_path = self.tmp_dir / f"{ctx.filename}.part{str(idx).zfill(len(str(ctx.split_count)))}"
                    if part_path.exists():
                        existing = part_path.read_bytes()
                        if math.isclose(len(existing), meta["bytes"], abs_tol=1):
                            if not meta["downloaded"]:
                                self._report_progress(int(meta["bytes"]), ctx)
                                meta["downloaded"] = True
                                with self._progress_lock:
                                    self._done_count += 1
                                    done = self._done_count
                                if not ctx.progress_bar.disable:
                                    ctx.progress_bar.desc = f"[{done}/{len(ranges)}] Downloaded"
                                continue
                        else:
                            part_path.unlink()

                    for thread_index in range(ctx.threads):
                        if self.url_locks[thread_index].locked():
                            continue
                        self.url_locks[thread_index].acquire()
                        meta["inUse"] = True
                        threading.Thread(
                            target=self._download_chunk,
                            args=(idx, meta, thread_index, ctx),
                            daemon=True,
                        ).start()
                        break
                time.sleep(0.05)
        except KeyboardInterrupt:
            self.cancel()
        finally:
            for lock in self.url_locks:
                if lock.locked():
                    lock.release()
            for lock in self.proxy_locks:
                if lock.locked():
                    lock.release()
            self._notify_proxy_state()

        return failed_chunk

    def _merge_parts(self, ranges: Dict[str, Dict[str, object]], filename: str, split_count: int) -> Path:
        target_path = Path(filename)
        if target_path.exists():
            target_path.unlink()

        with target_path.open("wb") as handle:
            for idx in range(len(ranges)):
                part_path = self.tmp_dir / f"{filename}.part{str(idx).zfill(len(str(split_count)))}"
                with part_path.open("rb") as chunk:
                    handle.write(chunk.read())
                part_path.unlink()

        self.log(f"Finished writing {filename}")
        self.log(f"File Size: {human_readable_bytes(target_path.stat().st_size)}")
        return target_path

    def _download_once(
        self,
        urls: Sequence[str],
        filename: str,
        threads: int,
        bytes_per_split: int,
    ) -> Path:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36"
        }
        total_size = self._fetch_total_size(urls[-1], headers)
        self._total_bytes = total_size
        self._bytes_downloaded = 0
        self._done_count = 0

        split_count = max(1, math.ceil(total_size / bytes_per_split))
        ranges = self._build_ranges(total_size, split_count)
        self._ranges_total = len(ranges)
        self.url_locks = [threading.Lock() for _ in range(threads)]

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
        )

        try:
            failed_chunk = self._run_scheduling_loop(ranges, ctx)
        finally:
            ctx.progress_bar.close()

        if failed_chunk is not None:
            idx, reason, attempts = failed_chunk
            raise ChunkDownloadFailed(
                f"Chunk {idx} failed after {attempts} attempts (last error: {reason}). "
                "The source IP and/or every proxy tried may be blocked or rate-limited."
            )

        if self.stop_event.is_set():
            raise DownloadCancelled("Download cancelled")

        return self._merge_parts(ranges, filename, split_count)

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
