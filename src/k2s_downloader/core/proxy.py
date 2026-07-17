from __future__ import annotations

import pathlib
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

import requests
from requests_futures.sessions import FuturesSession
from tqdm import tqdm

StatusCallback = Optional[Callable[[str], None]]

HTTPS_BATCH_SIZE = 50
HTTPS_TIMEOUT = 5.0
HTTPS_RETRIES = 1
# Timeout (seconds) for fetching a candidate list from one source (see
# _PROXY_SOURCES below). Larger than HTTPS_TIMEOUT because this is a single
# request returning a large text list, not a per-proxy liveness probe.
SOURCE_FETCH_TIMEOUT = 30

# R2-10: candidate sources, fetched concurrently and merged/deduplicated.
# Previously the only source was proxyscrape.com's now-deprecated v1 API
# (officially migrated to v2/v3 -- v1 could stop responding at any time,
# silently degrading the whole app to direct-connection-only with no clear
# error). Aggregating several independently-maintained lists both removes
# that single point of failure and meaningfully raises the fraction of
# candidates that are actually alive at any given moment. Each entry is
# (label, url); every source is best-effort -- one going down, rate-limiting
# us, or changing its response format never blocks the others.
_PROXY_SOURCES: List[tuple[str, str]] = [
    (
        "proxyscrape",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    ),
    ("TheSpeedX/PROXY-List", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("monosans/proxy-list", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    (
        "proxifly/free-proxy-list",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    ),
]

# Some sources (e.g. proxifly) prefix each entry with a URL scheme
# ("http://1.2.3.4:8080"); the rest of this module works with bare
# "host:port" strings, matching how they're plugged into the `proxies={...}`
# dict downstream (downloader.py always builds `http://{proxy}` itself).
_SCHEME_PREFIX_RE = re.compile(r"^\w+://")

# R2-10: how long a cached proxy list is trusted before a plain (non-forced)
# call to get_working_proxies() treats it as stale and revalidates instead
# of returning it as-is. Public proxies churn fast; without this, a list
# fetched once could be silently reused for days by a caller that never
# passes refresh=True or recheck_cached=True (e.g. the GUI's normal
# startup call).
PROXY_CACHE_TTL_SECONDS = 12 * 60 * 60

# R2-10: the previous validation target (api.myip.com) only proved a proxy
# could reach *some* HTTPS site -- not that it could reach Keep2Share, which
# is what actually matters. Shared public proxies are disproportionately
# likely to already be rate-limited or blocked by any one specific site
# (including k2s.cc itself), so validating directly against the real target
# filters out exactly the candidates that would otherwise pass validation
# and then fail on the very first chunk download.
PROXY_VALIDATION_URL = "https://k2s.cc/"


def _batched(items: List[str], size: int):
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _emit_status(callback: StatusCallback, message: str) -> None:
    if callback:
        callback(message)


def _normalize_candidate(line: str) -> str:
    return _SCHEME_PREFIX_RE.sub("", line.strip())


def _fetch_source(url: str) -> List[str]:
    response = requests.get(url, timeout=SOURCE_FETCH_TIMEOUT)
    response.raise_for_status()
    return [_normalize_candidate(line) for line in response.text.splitlines() if line.strip()]


def _fetch_all_sources(status_callback: StatusCallback) -> List[str]:
    collected: List[str] = []
    with ThreadPoolExecutor(max_workers=len(_PROXY_SOURCES)) as executor:
        future_to_label = {executor.submit(_fetch_source, url): label for label, url in _PROXY_SOURCES}
        for future in as_completed(future_to_label):
            label = future_to_label[future]
            try:
                collected.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - one bad source must never block the rest
                _emit_status(status_callback, f"Proxy source '{label}' unavailable ({exc}); skipping.")
    return collected


def get_working_proxies(
    refresh: bool = False,
    *,
    status_callback: StatusCallback = None,
    max_candidates: int | None = None,
    recheck_cached: bool = False,
    cache_path: pathlib.Path | str = "proxies.txt",
) -> List[Optional[str]]:
    """Return a list of working proxy endpoints.

    The first element is always ``None`` to represent a direct connection
    (``Downloader._acquire_proxy_lock`` relies on this and prefers it over
    any proxy). Results are cached at ``cache_path``, which defaults to
    ``proxies.txt`` in the current working directory for backward
    compatibility; pass an absolute path (e.g. a user data directory) to
    avoid writing into whatever directory the process happens to be run
    from.

    SECURITY: candidates come from several public, unauthenticated proxy
    lists (see ``_PROXY_SOURCES``) with no vetting beyond a basic HTTPS
    reachability check against the actual download target. A malicious or
    compromised proxy in this list can observe or tamper with traffic
    routed through it (see the MITM note where ``prox`` is built in
    downloader.py). Only use these for downloads where that risk is
    acceptable; prefer a direct connection or a trusted proxy of your own
    when it isn't.
    """

    cache_path = pathlib.Path(cache_path)
    cached_proxies: List[str] = []
    cache_is_stale = False
    if cache_path.exists():
        cached_proxies = [line for line in cache_path.read_text().splitlines() if line]
        cache_age = time.time() - cache_path.stat().st_mtime
        cache_is_stale = cache_age >= PROXY_CACHE_TTL_SECONDS

    if not refresh and not recheck_cached and cached_proxies and not cache_is_stale:
        return [None] + cached_proxies

    if cache_is_stale and not refresh and not recheck_cached and cached_proxies:
        # A plain (non-forced) call whose cache has aged out gets the
        # lighter-weight "revalidate what we already have" treatment
        # instead of a full re-fetch from every source -- same as if the
        # caller had explicitly passed recheck_cached=True.
        _emit_status(
            status_callback,
            f"Cached proxy list is older than {PROXY_CACHE_TTL_SECONDS // 3600}h; revalidating ...",
        )
        recheck_cached = True

    proxies: List[str] = []

    if recheck_cached and cached_proxies:
        _emit_status(status_callback, f"Revalidating {len(cached_proxies)} cached proxies ...")
        proxies.extend(cached_proxies)

    fetch_remote = False
    if recheck_cached:
        if not cached_proxies:
            _emit_status(status_callback, "No cached proxies to revalidate. Fetching fresh list ...")
            fetch_remote = True
    else:
        fetch_remote = refresh or not cached_proxies

    if fetch_remote:
        _emit_status(status_callback, f"Fetching proxy candidates from {len(_PROXY_SOURCES)} sources ...")
        proxies.extend(_fetch_all_sources(status_callback))

    # Remove duplicates while preserving order
    seen = set()
    deduped: List[str] = []
    for proxy in proxies:
        if proxy and proxy not in seen:
            deduped.append(proxy)
            seen.add(proxy)
    proxies = deduped

    if not proxies:
        _emit_status(status_callback, "No proxy candidates available.")
        if cached_proxies:
            return [None] + cached_proxies
        return [None]

    if max_candidates:
        proxies = proxies[:max_candidates]
        _emit_status(status_callback, f"Validating first {len(proxies)} proxies (limit {max_candidates}) ...")
    else:
        _emit_status(status_callback, f"Validating {len(proxies)} proxies ...")

    total_candidates = len(proxies)
    if total_candidates == 0:
        return [None]

    working_set: set[str] = set()
    https_progress = tqdm(total=total_candidates, desc="HTTPS check", unit="proxy") if status_callback is None else None

    remaining = list(proxies)
    attempt = 0
    validated_count = 0

    while remaining and attempt < HTTPS_RETRIES:
        attempt += 1
        next_remaining: List[str] = []
        if status_callback is not None:
            _emit_status(status_callback, f"HTTPS attempt {attempt}/{HTTPS_RETRIES} on {len(remaining)} proxies ...")

        for batch in _batched(remaining, HTTPS_BATCH_SIZE):
            session = FuturesSession(max_workers=min(len(batch), HTTPS_BATCH_SIZE))
            futures = []
            for proxy in batch:
                # HEAD, not GET: only connectivity to the real target matters
                # here, not its response body (see PROXY_VALIDATION_URL).
                future = session.head(
                    PROXY_VALIDATION_URL,
                    proxies={"https": f"http://{proxy}"},
                    timeout=HTTPS_TIMEOUT,
                )
                future.proxy = proxy  # type: ignore[attr-defined]
                futures.append(future)

            batch_success = 0
            for future in as_completed(futures):
                proxy_label = getattr(future, "proxy", "<unknown>")
                try:
                    response = future.result()
                    # A proxy that connects but gets a 4xx/5xx back (e.g. the
                    # target site's own block/rate-limit page) is just as
                    # useless for actually downloading as one that couldn't
                    # connect at all -- treat it as a failure too, not only
                    # a raised exception.
                    if response.status_code >= 400:
                        raise requests.exceptions.HTTPError(
                            f"status {response.status_code}", response=response
                        )
                    if proxy_label not in working_set:
                        working_set.add(proxy_label)
                        batch_success += 1
                except KeyboardInterrupt:
                    raise
                except Exception:
                    if attempt < HTTPS_RETRIES:
                        next_remaining.append(proxy_label)
                finally:
                    validated_count += 1
                    if status_callback is not None and validated_count % 1000 == 0:
                        _emit_status(
                            status_callback,
                            f"HTTPS validated {validated_count} proxy attempts so far ...",
                        )

            session.close()
            if https_progress:
                https_progress.update(batch_success)
            sleep_time = random.uniform(0.2, 0.4)
            time.sleep(sleep_time)

        if status_callback is not None and next_remaining:
            _emit_status(status_callback, f"HTTPS pending {len(next_remaining)} proxies after attempt {attempt}.")
        remaining = next_remaining

    if https_progress:
        https_progress.close()

    working_proxies = list(working_set)
    if not working_proxies:
        _emit_status(status_callback, "No proxies passed HTTPS validation.")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("")
        return [None]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("\n".join(working_proxies))
    _emit_status(status_callback, f"Found {len(working_proxies)} working proxies.")

    return [None] + working_proxies
