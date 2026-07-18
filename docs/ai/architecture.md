# K2S Downloader — Architecture

> Canonical, AI-facing architecture document. Human-facing Traditional Chinese counterpart: [docs/human/architecture.md](../human/architecture.md). Reading order for this repo: [AGENTS.md](../../AGENTS.md) → [requirements.md](requirements.md) → this file → [todolist.md](todolist.md). For setup/usage instructions see the human-facing [Readme.md](../../Readme.md); for contribution workflow see [CONTRIBUTING.md](../../CONTRIBUTING.md).

## 1. Module Map

```
src/k2s_downloader/
├── __main__.py          # `python -m k2s_downloader` -> cli.main()
├── cli.py                # argparse CLI front end
├── core/                 # No GUI-toolkit dependency. Importable/testable standalone.
│   ├── downloader.py      # Downloader: orchestration, range-splitting, chunk scheduling/download, merge
│   ├── k2s_client.py      # Keep2Share API client: captcha, URL generation, filename lookup
│   └── proxy.py           # Public proxy list fetch/validation/caching
└── gui/                  # PySide6 front end. Depends on core/ only through its callback interface.
    ├── app.py              # QApplication bootstrap, stylesheet/icon loading
    ├── main_window.py      # MainWindow: all widgets, signal wiring, UI-thread state
    ├── worker.py           # DownloadWorker / ProxyLoaderWorker: QThread wrappers around core.Downloader
    └── paths.py            # app_data_dir() / default_download_dir() via QStandardPaths (R2-9)

k2s_gui_entry.py          # Thin script entry point -> gui.app.main() (for PyInstaller builds)
tests/                    # Mirrors core/ modules; mocks all `requests` calls; no GUI tests (see NFR-4)
```

`core/` is the only package with business logic. `cli.py` and `gui/` are both thin front ends that construct a `Downloader`, wire up callbacks/signals, and otherwise get out of the way.

**File locations** (R2-9): `Downloader`'s `tmp_dir`/`url_cache_path`/`proxy_cache_path` constructor params default to bare relative names (`tmp`, `urls.json`, `proxies.txt`), resolved against the process CWD — fine for the CLI (CWD is wherever the user chose to run it from) but not for a double-clicked GUI exe, whose CWD is the exe's own install directory and may not be writable (e.g. under `Program Files`). `gui/paths.py::app_data_dir()` resolves a per-user writable folder via `QStandardPaths.AppDataLocation` (requires `QApplication.setOrganizationName`/`setApplicationName`, set in `gui/app.py::main()` before any GUI code runs); `gui/worker.py::DownloadWorker.run()`/`ProxyLoaderWorker.run()` pass that folder's `tmp`/`urls.json`/`proxies.txt` subpaths into `Downloader`/`get_working_proxies()` instead of relying on their CWD-relative defaults. Separately, `Downloader.download()` takes an optional `output_dir` (applied via `_apply_output_dir`, joined with only the final path component of the resolved filename — any directory component the filename itself carries is discarded so it can't escape a caller-chosen `output_dir`) so the GUI's "save to" folder picker (`main_window.py`, defaulting to `QStandardPaths.DownloadLocation`) controls where the *finished* file lands, independent of the app-data tmp/cache location above. The CLI never passes `output_dir`, so its existing CWD-relative behavior (including `--filename out/video.mp4`-style directory components, R2-5) is unchanged.

## 2. Control Flow

```
CLI/GUI
  └─ Downloader.download(url, ...)
       ├─ stop_event.clear()                              # AC-7.2
       ├─ refresh_proxies()                                 # if not already populated
       ├─ extract_file_id(url)                              # REQ-1
       ├─ k2s_client.get_name(file_id)                       # REQ-2 (filename)
       ├─ k2s_client.generate_download_urls(...)              # REQ-5 (captcha) + one URL per thread
       │     ├─ fetch_captcha() -> captcha_callback(...)
       │     └─ per proxy: solve captcha -> free_download_key -> batch-fetch `count` URLs
       └─ loop (at most twice, for the media-recheck retry in AC-8.2):
             └─ _download_once(urls, filename, threads, split_size)
                  ├─ _fetch_total_size(url, headers)          # REQ-2 (size, HEAD request)
                  ├─ _build_ranges(total_size, split_count)     # REQ-3 (contiguous byte ranges)
                  ├─ _prepare_resume(...)                       # REQ-11 (skip already-completed ranges)
                  └─ _run_scheduling_loop(ranges, ...)          # REQ-3/4 (dispatch + retry/backoff)
                        └─ per range, in its own thread:
                              _download_chunk(...)
                                ├─ _acquire_proxy_lock()         # REQ-6 (direct-first)
                                ├─ requests.get(Range: bytes=...)
                                ├─ on success: write .partNNN, mark range done
                                └─ on failure: _mark_chunk_failed() -> backoff or permanent failure (ChunkDownloadFailed
                                   propagates straight out of download() -- see R2-16 in todolist.md for why this
                                   isn't auto-retried at the whole-download level)
                  └─ _merge_parts(ranges, filename, split_count)  # REQ-3.6 (concatenate + cleanup)
```

`download()` is the only public entry point front ends call. Everything under `_download_once` is private to `Downloader` and exists to keep that method itself short — see § 5.

## 3. Threading & Concurrency Model

`Downloader` runs its own scheduling loop on the calling thread (blocking `download()` until the file is complete, cancelled, or failed) and spawns one short-lived `threading.Thread` per in-flight chunk, up to `threads` concurrently. There is no thread pool; threads are created and left to finish/exit on their own (`daemon=True`), gated by acquiring one of a fixed set of `url_locks` before starting.

| Shared state | Guarded by | Notes |
|---|---|---|
| `working_proxy_indexes` (list of proxy indices known to work) | `_working_proxy_lock` | Read (copied) and appended from multiple chunk threads; a dedicated lock replaced a prior race where this list was mutated with no synchronization at all. |
| `proxy_locks[i]` (one lock per proxy) | itself | Acquired via non-blocking `acquire(blocking=False)` in a retry loop (`_acquire_proxy_lock`), never a blocking acquire — see § 4 for why this matters. |
| `_active_proxy_indexes` | `_proxy_state_lock` | Read for UI/status snapshotting (`_notify_proxy_state`); written on chunk start/end. |
| `_bytes_downloaded`, `_done_count` | `_progress_lock` | Read-modify-write from any chunk thread via `_report_progress` / `_download_chunk`. |
| `url_locks[i]` (one per thread slot) | itself | Gates how many chunk threads may be in flight at once; acquired by the scheduling loop before spawning a thread, released by that thread (or the `finally` cleanup in `_run_scheduling_loop`/`_download_once` on early exit). |
| `stop_event` (`threading.Event`) | n/a — already thread-safe | The single source of truth for cancellation; see § 4. |

### Why cancellation is a single `Event`, not a mirrored boolean

`_download_chunk` and the scheduling loop both need to know "has this download been cancelled?" `_download_chunk` previously mirrored this into a `nonlocal stop` boolean shared via closure across all chunk threads. Every code path that set it did so only when `self.stop_event` was already set (or being set in the same statement) — so it was fully redundant with, and riskier than, just checking `self.stop_event.is_set()` directly. The current implementation does exactly that: `_download_chunk` never touches a stop flag at all; `_run_scheduling_loop` keeps one *local, single-threaded* `stop_scheduling` boolean, but only to also cover a second, distinct exit condition (a chunk permanently exhausting its retry budget) that has nothing to do with `stop_event`.

### Why `_acquire_proxy_lock` never blocks

The previous implementation scanned for a proxy lock reporting `locked() == False` and then called a *blocking* `.acquire()` on it — a check-then-act race: two threads could observe the same lock as free, and the loser would then block indefinitely inside `.acquire()` while still holding its `url_locks` slot, effectively deadlocking that thread slot. `_acquire_proxy_lock` instead loops calling `acquire(blocking=False)` (never blocks) with a short sleep between attempts, and bails out (`return None`) once `stop_event` is set — guaranteeing forward progress under contention and prompt cancellation response even when every proxy is momentarily busy.

## 4. Proxy Handling Design

`proxy.py::get_working_proxies()` is the sole source of proxy candidates. Its return value always has `None` as element 0 (meaning "no proxy, direct connection") — this invariant is load-bearing: `Downloader._acquire_proxy_lock()` checks `self.proxies[0] is None` and always tries to acquire that slot first, before falling back to any known-good or randomly-chosen proxy.

**Sourcing and validation (R2-10)**: candidates are fetched concurrently from several independently-maintained public lists (`_PROXY_SOURCES`: proxyscrape's v2 API plus three GitHub-hosted raw lists — `TheSpeedX/PROXY-List`, `monosans/proxy-list`, `proxifly/free-proxy-list`), merged and deduplicated; each source is best-effort, so one going down, rate-limiting, or changing its response format never blocks the others (previously the sole source was proxyscrape's now-deprecated v1 API, a single point of failure that could silently degrade the app to direct-connection-only). Candidates are validated with an HTTPS `HEAD` request against the real download target (`PROXY_VALIDATION_URL = "https://k2s.cc/"`, not a generic reachability check like the previous `api.myip.com`) and a non-2xx/3xx response is treated as a validation failure the same as a connection error — a proxy that's reachable but already blocked/rate-limited by Keep2Share specifically is exactly the kind of candidate this is meant to filter out. Validated results are cached to disk (`cache_path`, configurable, defaulting to `proxies.txt` in the CWD); a plain call (`refresh=False`, `recheck_cached=False`) returns the cache as-is only while it's fresher than `PROXY_CACHE_TTL_SECONDS` (12h) — once stale it's automatically treated like `recheck_cached=True` (revalidate what's cached, not a full re-fetch from every source) instead of being trusted indefinitely.

**Runtime degradation (R2-10)**: `Downloader` tracks consecutive chunk-download failures per proxy index (`_proxy_consecutive_failures`, guarded by `_working_proxy_lock` alongside `working_proxy_indexes`) via `_note_proxy_failure`, called from `_mark_chunk_failed`'s optional `proxy_idx` parameter. Past `PROXY_FAILURE_EVICTION_THRESHOLD` (3) consecutive failures, the proxy is dropped from `working_proxy_indexes` (previously indexes were only ever appended, never removed, so a proxy that degraded mid-download kept being preferentially reselected for the rest of the run). Eviction only removes it from the "known good" tier `_acquire_proxy_lock` checks first — it's still reachable via the random-fallback tier, and a later success clears its failure count and re-adds it to `working_proxy_indexes`, so a proxy that recovers becomes eligible again rather than being permanently blacklisted. Index 0 (direct connection) is exempt from all of this, since `_acquire_proxy_lock` always tries it first unconditionally regardless of list membership.

Security posture (see also [Readme.md](../../Readme.md)'s "Security Note: Public Proxies" section): the proxy hop itself is unauthenticated plain HTTP even when the target URL is HTTPS (`requests` tunnels HTTPS through an HTTP `CONNECT` to the proxy), so a malicious proxy operator is positioned to observe or tamper with routed traffic. This is a deliberate, documented tradeoff for working around per-IP rate limits, not an oversight — direct connection is always preferred (§ 3) and the proxy pool is purely a fallback.

**Throughput telemetry** (see [`todolist.md`](todolist.md) R2-11): `_report_progress` optionally takes `is_direct: bool` from `_download_chunk` (`proxy_idx == 0`), crediting live-connection bytes to `_direct_bytes_downloaded` or `_proxy_bytes_downloaded`; bytes credited without a live connection this run (the scheduling loop's part-reuse branch resuming an already-on-disk part) pass `is_direct=None` and are excluded from that split. `_run_scheduling_loop` polls `_maybe_report_throughput()` every tick, which emits a `status_callback` message (aggregate speed, direct/proxy percentage split, active connection count) at most once per `TELEMETRY_REPORT_INTERVAL` (5s). This exists to let real usage validate or refute the working hypothesis that this project's speed limit is per-connection rather than per-IP — the instrumentation alone doesn't decide anything; R2-10/R2-12's proxy-investment and tail-collapse decisions still need real observed data.

## 5. Error Taxonomy

| Exception | Raised when | Caller-visible meaning |
|---|---|---|
| `DownloadCancelled` | `stop_event` was set (via `Downloader.cancel()`) at any point during `download()` | "The user/caller stopped this." Not an error. |
| `ChunkDownloadFailed` | A single byte-range chunk exhausted `MAX_CHUNK_RETRIES` (25) attempts | "We gave up." Distinguished from `DownloadCancelled` so callers can render different UI/exit codes for "you stopped it" vs. "it couldn't complete." Deliberately *not* auto-retried at the whole-download level — see R2-16 in `todolist.md`. |
| `K2SFileNotFound` | Keep2Share's API reports the file doesn't exist | Catchable, non-fatal — replaces a previous `sys.exit()` that used to kill the entire process (including a hosting GUI) from a background thread. |
| `OperationCancelled` (in `k2s_client`) | `stop_event` was set during captcha/URL-generation, before any chunk download started | Caught by `Downloader.download()` and re-raised as `DownloadCancelled` so callers only ever need to handle one cancellation exception type regardless of which phase was cancelled. |
| `RuntimeError` (various messages) | Network-unreachable during size/name lookup; captcha rejected `MAX_CAPTCHA_ATTEMPTS` times; every proxy exhausted with zero working URLs; size undeterminable | Each message names the likely cause (blocked IP, rate limit, etc.) rather than surfacing a bare `requests` exception. |
| `ValueError` | Invalid URL (`extract_file_id`); `split_size` below the 5 MiB floor; unparseable `--split-size` string | Caller/input-validation errors, raised before any network activity. |

Retry/backoff parameters (module-level constants in `downloader.py` / `k2s_client.py`, intentionally centralized rather than inlined — see [`todolist.md`](todolist.md) P2-2):

| Constant | Value | Governs |
|---|---|---|
| `MAX_CHUNK_RETRIES` | 25 | Attempts per byte-range chunk before `ChunkDownloadFailed` (raised 8 -> 25 in R2-16's revert -- see `todolist.md`) |
| `CHUNK_RETRY_BACKOFF_BASE` / `_CAP` | 1.0s / 30.0s | Exponential backoff between chunk retry attempts |
| `MAX_CAPTCHA_ATTEMPTS` | 3 | Rejected captcha answers before giving up |
| `MAX_URL_BATCH_ROUNDS` | 3 | Consecutive zero-progress rounds fetching download URLs from one proxy before trying the next |
| `PROXY_FAILURE_EVICTION_THRESHOLD` | 3 | Consecutive chunk failures via one proxy before it's evicted from `working_proxy_indexes` (R2-10) |
| `PROXY_CACHE_TTL_SECONDS` | 12h | Age at which a cached proxy list is treated as stale and revalidated instead of returned as-is (R2-10, `proxy.py`) |

### Timeout inventory

Every outbound HTTP call in this project carries an explicit timeout (NFR-1); no call relies on `requests`' default (no timeout at all, i.e. block forever).

| Constant | Module | Value | Purpose |
|---|---|---|---|
| `HEAD_REQUEST_TIMEOUT` | `downloader.py` | 15s | Discovering total file size |
| `CHUNK_REQUEST_TIMEOUT` | `downloader.py` | 20s | Connect/read timeout for each chunk's `GET` |
| `CHUNK_STALL_TIMEOUT` | `downloader.py` | 20s | Separate stall watchdog: abandon a chunk attempt if no new bytes arrive within this window, even though the socket itself hasn't timed out |
| `DEFAULT_TIMEOUT` | `k2s_client.py` | 15s | General Keep2Share API calls (captcha fetch, filename lookup, URL batch generation) |
| `CAPTCHA_SOLVE_TIMEOUT` | `k2s_client.py` | 5s | The per-proxy captcha-solve probe specifically — deliberately shorter so one dead proxy doesn't stall the whole captcha loop |
| `HTTPS_TIMEOUT` | `proxy.py` | 5s | Per-candidate reachability probe during proxy validation |
| `SOURCE_FETCH_TIMEOUT` | `proxy.py` | 30s | Fetching a candidate list from one source (one larger request, not a per-candidate probe) |

## 6. GUI Integration

`gui/worker.py` wraps `core.Downloader` in two `QThread` subclasses so the Qt event loop is never blocked by network I/O:

- **`DownloadWorker`**: owns one `Downloader` instance for the lifetime of one download. Bridges `Downloader`'s plain-callback interface (`status_callback`, `progress_callback`, `proxy_state_callback`, `captcha_callback`) to Qt signals. Progress and proxy-state updates are throttled (`_progress_emit_interval`, `_proxy_emit_interval`) before crossing the thread boundary — this is what AC-10.2 refers to; without it, a high thread count would flood the UI thread with signal emissions and freeze it. The captcha callback specifically blocks the worker thread on a `threading.Event` until `MainWindow.submit_captcha()` sets it from the UI thread.
- **`ProxyLoaderWorker`**: a separate, simpler `QThread` for the "refresh proxy list" action, so it doesn't block the UI or require an active download.

`main_window.py`'s `MainWindow` owns all widgets and holds UI-thread-only state (smoothed download-rate/ETA, log buffer). It never touches `Downloader` directly — only through a `DownloadWorker` instance it creates per download and tears down in `_reset_state()`.

## 7. Testing Structure

`tests/` mirrors `core/` by concern, not strictly by file:

- `test_downloader_units_and_ranges.py` — pure-function/unit tests: `parse_size`, `_build_ranges`, `_acquire_proxy_lock` concurrency safety.
- `test_downloader_error_handling.py` — chunk-level exception handling (request errors vs. unexpected errors, both recorded not swallowed).
- `test_downloader_status_code.py` / `test_downloader_timeouts.py` / `test_downloader_retry_limit.py` — HTTP status handling, timeout propagation, retry/backoff exhaustion.
- `test_k2s_client_timeouts.py` / `test_k2s_client_blocked.py` — captcha/URL-generation timeout and bounded-retry behavior.
- `test_proxy_preference_and_cache.py` — direct-connection preference, configurable cache path, `get_working_proxies`'s cached/refresh/recheck_cached paths.
- `test_human_readable_bytes.py` — display-formatting unit conversion.

Every test mocks `requests` at the call site (`patch("k2s_downloader.core.downloader.requests.get", ...)` etc.) — none make real network calls. `gui/` has no test coverage by design (`# pragma: no cover - GUI wiring`); it requires a running Qt application to exercise meaningfully, and its logic is intentionally kept thin (state bridging only, no business logic) so this gap is low-risk.

## 8. CI

`.github/workflows/ci.yml` runs `ruff check .` and `pytest -q` on every push to `main` and every pull request, against Python 3.9 (the `requires-python` floor) and 3.13. `.github/workflows/ai-review.yml` separately posts an LLM-generated review comment on pull requests (unrelated to the test/lint gate).
