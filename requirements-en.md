# K2S Downloader — Requirements

> Canonical, AI-facing requirements document. Human-facing Traditional Chinese counterpart: [requirements-zh.md](requirements-zh.md). Reading order for this repo: this file → [readme-en.md](readme-en.md) (architecture) → other docs.

## 1. Purpose

A parallel-download client for Keep2Share (`k2s.cc`) file links. Splits a single file into byte-range chunks, downloads them concurrently (optionally through a pool of third-party HTTPS proxies to work around per-IP rate limiting), and reassembles them into the target file. Ships two front ends over the same core engine: a CLI (`k2s-downloader`) and a PySide6 desktop GUI (`k2s-downloader-gui`).

## 2. Scope

**In scope**: downloading a single Keep2Share file per invocation given its share URL; captcha-gated authorization against Keep2Share's public API; parallel chunked transfer with retry/backoff; optional proxy pool for IP diversity; optional post-download media-integrity check via `ffmpeg`.

**Out of scope**: batch/queue downloading of multiple URLs in one run; account-based (non-free) Keep2Share access tiers; resuming a download across separate process invocations without the on-disk `.partNNN` files already present; any Keep2Share upload functionality; bypassing or automating captcha solving (the tool surfaces the captcha to the user/caller — see REQ-5).

## 3. Actors

- **End user** — runs the CLI or GUI to download a file.
- **Library caller** — imports `k2s_downloader.core` directly (e.g. to supply a custom `captcha_callback` for automation) instead of using either front end.
- **Keep2Share API** (`k2s.cc`) — external, third-party, not controlled by this project. Its captcha/rate-limit/error-message contract is treated as fixed and is duck-typed against in `core/k2s_client.py`.
- **Third-party HTTPS proxies** (via `proxyscrape.com`) — untrusted, optional infrastructure; see the security notes under REQ-6.

## 4. Functional Requirements

### REQ-1: Accept and validate a Keep2Share URL
- **AC-1.1**: Given a URL matching `https?://(k2s.cc|keep2share.cc)/file/<id>...`, the file ID is extracted successfully.
- **AC-1.2**: Given a URL that does not match that pattern, a `ValueError` is raised before any network call is made.

### REQ-2: Discover the file's display name and total size
- **AC-2.1**: The original filename is fetched via the Keep2Share `getFilesInfo` API before any chunk download begins.
- **AC-2.2**: If the caller supplied an explicit filename without an extension, the original file's extension is appended to it; a filename with its own extension is used as-is.
- **AC-2.3**: Total size is discovered via an HTTP `HEAD` request's `Content-Length` header before chunking; a negative `Content-Length` (32-bit signed overflow observed from some CDNs) is corrected by adding 2³².
- **AC-2.4**: A network failure during either lookup raises a `RuntimeError` with a message indicating the host may be blocking/rate-limiting the request, not a raw exception or an indefinite hang (both calls carry an explicit timeout).

### REQ-3: Split the file into byte ranges and download them concurrently
- **AC-3.1**: The file is split into `ceil(total_size / split_size)` ranges (minimum 1), each contiguous with its neighbors — no byte is left out of every range (a gap) and no byte is claimed by two ranges (an overlap) for any valid `(total_size, split_count)` pair.
- **AC-3.2**: Up to `threads` ranges download concurrently, each in its own thread, gated by a fixed-size pool of per-thread "url locks".
- **AC-3.3**: `split_size` must be at least 5 MiB; a smaller value raises `ValueError` before any download starts.
- **AC-3.4**: A range whose downloaded byte count doesn't match its expected size (within 1 byte) is treated as failed and retried, not silently accepted.
- **AC-3.5**: An already-downloaded `.partNNN` file on disk whose size matches the expected range size is reused without re-downloading (crash/restart resilience within a single `download()` call's temp directory).
- **AC-3.6**: On success, all part files are concatenated in range order into the final target file and the temp part files are deleted; a pre-existing file at the target path is overwritten.

### REQ-4: Bounded retry with backoff, not indefinite hangs
- **AC-4.1**: A chunk that fails (non-2xx HTTP status, network-level exception, or a byte-count mismatch) is retried with exponential backoff (base 1s, capped at 30s) up to a fixed maximum attempt count (8), after which the whole download aborts with a `ChunkDownloadFailed` naming the range, attempt count, and last recorded failure reason.
- **AC-4.2**: URL-generation (the captcha/proxy-probing phase before any chunk download starts) has its own bounded retry: at most 3 rejected captcha answers, and at most 3 consecutive rounds of zero progress fetching download URLs from a given proxy before moving to the next proxy; exhausting every proxy without success raises a `RuntimeError` explaining the IP/proxy pool is likely blocked, rather than hanging.
- **AC-4.3**: Every outbound HTTP request made by this project carries an explicit timeout; none may block indefinitely on an unresponsive host.
- **AC-4.4**: A failure inside a single chunk-download thread is always recorded via the same failure-accounting path (never left silently swallowed) and never leaves that range's "in use" flag stuck, which would otherwise stall the scheduling loop on that range forever.

### REQ-5: Captcha handling
- **AC-5.1**: Before any download URL is generated, a captcha challenge is fetched and handed to a caller-supplied `captcha_callback(image_bytes, challenge, captcha_url) -> str`; the default implementation (used by the CLI) opens the image and reads a response from stdin. The GUI supplies its own callback that blocks the background worker thread on a Qt signal round-trip until the user submits a response in the UI.
- **AC-5.2**: An incorrect captcha response triggers a fresh challenge and callback invocation, up to `MAX_CAPTCHA_ATTEMPTS` (3) total attempts, after which a `RuntimeError` is raised explaining the answers may have been correct but the IP is likely blocked.
- **AC-5.3**: A "File not found" response from Keep2Share raises a catchable `K2SFileNotFound` exception — not a process-terminating call — so a GUI host is not torn down by a bad link.

### REQ-6: Optional proxy pool for chunk downloads
- **AC-6.1**: `Downloader.refresh_proxies()` populates a proxy list whose first entry is always `None` (meaning "direct connection, no proxy"); this invariant is relied on elsewhere (AC-6.2) and is guaranteed by every return path of `get_working_proxies()`.
- **AC-6.2**: When selecting a connection for a chunk, the direct connection is always attempted first if free; a third-party proxy is used only when the direct slot is unavailable/busy.
- **AC-6.3**: Proxy candidates are sourced from a public, unauthenticated third-party list (proxyscrape.com) and only lightly vetted (a single reachability probe against `api.myip.com`); this is documented to the user as a MITM risk (the proxy hop itself is plain, unauthenticated HTTP even when the target URL is HTTPS) rather than presented as a trusted channel.
- **AC-6.4**: The proxy candidate cache path is configurable (`Downloader(proxy_cache_path=...)` / `get_working_proxies(cache_path=...)`), defaulting to `proxies.txt` in the current working directory for backward compatibility; writing to a path whose parent directory doesn't yet exist succeeds (parents are created automatically).
- **AC-6.5**: Proxy selection across concurrent chunk-download threads never allows two threads to hold the same proxy simultaneously, and a thread waiting for a proxy slot gives up promptly (does not busy-loop indefinitely) once the download is cancelled.

### REQ-7: Cancellation
- **AC-7.1**: Calling `Downloader.cancel()` at any point during `download()` — including during captcha solving, URL generation, or chunk downloading — causes the call to raise `DownloadCancelled` rather than continuing to completion or hanging.
- **AC-7.2**: A cancellation flag set before a `download()` call begins does not leak into that call (the flag is cleared at the very start of `download()`, before any network activity, so a stale cancellation from a previous run can't silently no-op the new one).

### REQ-8: Optional post-download media integrity check
- **AC-8.1**: If `ensure_media_check` is enabled (default), the target extension is a known media type, and `ffmpeg` is available on `PATH`, the downloaded file is probed for corruption via `ffmpeg -c copy -f null`.
- **AC-8.2**: A corrupted result triggers exactly one automatic re-download attempt at double the original split size before giving up and logging that the file is still corrupted; it does not retry indefinitely.
- **AC-8.3**: If `ffmpeg` is unavailable, or the file extension isn't a recognized media type, or the check is disabled, the download completes without this step (not an error condition).

### REQ-9: CLI interface
- **AC-9.1**: `k2s-downloader <url> [--filename NAME] [--threads N] [--split-size SIZE] [--no-ffmpeg-check]` performs a single download to completion or a clear, non-zero-exit failure.
- **AC-9.2**: `--split-size` accepts binary (IEC) size suffixes case-insensitively — bare `B`, single-letter `K`/`M`/`G`/`T`, two-letter `KB`/`MB`/`GB`/`TB`, and three-letter `KIB`/`MIB`/`GIB`/`TIB` — all resolving to powers of 1024, not 1000; an unrecognized suffix raises a user-facing `ValueError`-derived error (argparse `error()`), never an uncaught exception.
- **AC-9.3**: The CLI's own `--split-size` default (`"20M"`) is itself valid input to the same parser used for `--split-size` values the user supplies (regression: this previously was not the case and crashed every default invocation).

### REQ-10: GUI interface
- **AC-10.1**: The GUI exposes the same download parameters as the CLI (URL, filename override, thread count, split size, media-check toggle) plus proxy-pool controls (refresh, revalidate cached, candidate limit) and a developer panel showing live proxy availability/activity.
- **AC-10.2**: Progress, status log lines, and proxy-state updates from the background download thread are throttled before reaching the UI thread (a fixed tick interval), so a high thread count does not freeze the UI with excessive signal traffic.
- **AC-10.3**: A captcha requirement surfaces inline in the GUI (image + text field) rather than blocking on a terminal prompt; submitting unblocks the background download thread.
- **AC-10.4**: Closing the main window while a download is in progress cancels it and waits (bounded) for the worker thread to stop before the process exits.

## 5. Non-Functional Requirements

- **NFR-1 (Timeouts)**: every outbound network call has an explicit, named timeout constant (see `readme-en.md` § Timeout inventory); none rely on library defaults.
- **NFR-2 (Concurrency safety)**: shared mutable state touched from multiple chunk-download threads (the working-proxy index list, active-proxy set, progress counters) is protected by dedicated locks; no field is read-modified-written from more than one thread without one.
- **NFR-3 (Portability)**: `core/` has no GUI-toolkit dependency; it must remain importable and independently testable without PySide6 installed.
- **NFR-4 (Test coverage)**: every bug fix lands with a regression test under `tests/` that fails against the pre-fix behavior; GUI wiring in `gui/` is the sole documented exception (`# pragma: no cover - GUI wiring`).
- **NFR-5 (Platform)**: developed and tested on Windows with Python 3.13; `pyproject.toml` declares a floor of Python 3.9 and CI additionally runs on that floor.

## 6. Known Limitations (as of this document)

- The proxy pool's trust model is "best-effort, not secure" by design (see AC-6.3) — this is a documented tradeoff, not a defect, but it means this tool is unsuitable for downloads where proxy-hop confidentiality/integrity matters.
- `--threads`/GUI thread count is not automatically capped to the number of URLs Keep2Share is willing to issue; if fewer URLs are available than requested, the effective thread count is silently reduced (AC-3.2 still holds, just against the reduced count) and a status message notes the reduction.
- There is no persistent download queue/history; each invocation is a single, independent download.
