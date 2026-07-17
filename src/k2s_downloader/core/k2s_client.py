from __future__ import annotations

import threading
import time
from concurrent.futures import as_completed
from io import BytesIO
from random import choice
from typing import Callable, List, Optional, Sequence

import requests
from PIL import Image
from requests_futures.sessions import FuturesSession
from tqdm import tqdm

from .proxy import get_working_proxies

CaptchaCallback = Callable[[bytes, str, str], str]
StatusCallback = Optional[Callable[[str], None]]

DOMAINS = ["k2s.cc"]

# Default timeout (seconds) applied to every outbound request in this module.
# Without a timeout, a blocked/black-holed IP causes requests to hang
# indefinitely, which is what makes the CLI/GUI appear to freeze.
DEFAULT_TIMEOUT = 15

# How many captcha solves the user gets before we give up. Previously an
# "Invalid captcha code" response re-prompted forever, so a blocked IP whose
# captcha submissions were being rejected looked like an app that "does
# nothing" after entering the captcha.
MAX_CAPTCHA_ATTEMPTS = 3

# How many consecutive batch rounds of getUrl may yield zero new URLs before
# we conclude the key/proxy is blocked. Previously ``while len(urls) < count``
# spun forever when every request failed, which was the main "entered the
# captcha but nothing happens" hang.
MAX_URL_BATCH_ROUNDS = 3

# Timeout (seconds) for the getUrl request made while solving the captcha
# and probing each proxy in generate_download_urls. Deliberately shorter
# than DEFAULT_TIMEOUT so a single dead proxy doesn't stall the captcha
# loop for the full 15s before the next iteration is attempted.
CAPTCHA_SOLVE_TIMEOUT = 5


class OperationCancelled(RuntimeError):
    """Raised when the caller requested cancellation via ``stop_event``."""


class K2SFileNotFound(RuntimeError):
    """Raised when the Keep2Share API reports the file does not exist."""


def _raise_if_cancelled(stop_event: Optional[threading.Event]) -> None:
    if stop_event is not None and stop_event.is_set():
        raise OperationCancelled("Cancelled while generating download URLs")


def _emit_status(callback: StatusCallback, message: str) -> None:
    if callback:
        callback(message)


def default_captcha_callback(image_bytes: bytes, challenge: str, captcha_url: str) -> str:
    image = Image.open(BytesIO(image_bytes))
    image.show()
    return input("Enter captcha response: ")


def fetch_captcha(status_callback: StatusCallback = None) -> dict:
    _emit_status(status_callback, "Requesting captcha challenge...")
    response = requests.post(f"https://{choice(DOMAINS)}/api/v2/requestCaptcha", timeout=DEFAULT_TIMEOUT)
    return response.json()


def generate_from_key(
    url: str,
    key: str,
    proxy: Optional[str],
    *,
    status_callback: StatusCallback = None,
    max_retries: int = 5,
) -> str:
    """Exchange a free_download_key for a real download URL.

    Retries up to ``max_retries`` times with a short backoff instead of
    looping forever: previously any failure (including a timeout) was
    silently swallowed and retried immediately with no upper bound, which
    could spin/hang indefinitely if the IP was blocked.
    """
    prox = {"https": f"http://{proxy}"} if proxy else None

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                f"https://{choice(DOMAINS)}/api/v2/getUrl",
                json={"file_id": url, "free_download_key": key},
                proxies=prox,
                timeout=DEFAULT_TIMEOUT,
            ).json()
            return response["url"]
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - network/parse errors are all retryable here
            last_error = exc
            _emit_status(
                status_callback,
                f"generate_from_key attempt {attempt}/{max_retries} failed: {exc}",
            )
            if attempt < max_retries:
                time.sleep(min(2**attempt, 10))

    raise RuntimeError(f"Failed to generate URL from key after {max_retries} attempts") from last_error


def generate_download_urls(
    file_id: str,
    count: int = 1,
    *,
    skip: int = 0,
    proxies: Optional[Sequence[Optional[str]]] = None,
    captcha_callback: Optional[CaptchaCallback] = None,
    status_callback: StatusCallback = None,
    stop_event: Optional[threading.Event] = None,
) -> List[str]:
    """Collect temporary download URLs for the given file identifier.

    ``stop_event`` (if given) is checked between network operations so the
    caller can abort this phase; previously cancellation only took effect
    once the actual chunk download had started.
    """

    _raise_if_cancelled(stop_event)

    proxy_pool: Sequence[Optional[str]]
    if proxies is None:
        proxy_pool = get_working_proxies()
    else:
        proxy_pool = proxies

    if skip > 0:
        proxy_pool = proxy_pool[skip:]

    captcha_callback = captcha_callback or default_captcha_callback

    working_link = False
    free_download_key = ""
    urls: List[str] = []
    captcha_attempts = 1

    try:
        captcha = fetch_captcha(status_callback)
        captcha_image = requests.get(captcha["captcha_url"], timeout=DEFAULT_TIMEOUT).content
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Could not fetch a captcha challenge (network unreachable or IP blocked): {exc}"
        ) from exc
    response = captcha_callback(captcha_image, captcha["challenge"], captcha["captcha_url"])

    for proxy in proxy_pool:
        _raise_if_cancelled(stop_event)
        label = proxy or "LOCAL"
        _emit_status(status_callback, f"Trying proxy {label}")
        prox = {"https": f"http://{proxy}"} if proxy else None

        while not working_link:
            _raise_if_cancelled(stop_event)
            try:
                free_r = requests.post(
                    f"https://{choice(DOMAINS)}/api/v2/getUrl",
                    json={
                        "file_id": file_id,
                        "captcha_challenge": captcha["challenge"],
                        "captcha_response": response,
                    },
                    proxies=prox,
                    timeout=CAPTCHA_SOLVE_TIMEOUT,
                ).json()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                _emit_status(status_callback, f"[{label}] request failed ({exc}); trying next proxy.")
                break

            if free_r.get("status") == "error":
                message = free_r.get("message", "")
                if message == "Invalid captcha code":
                    if captcha_attempts >= MAX_CAPTCHA_ATTEMPTS:
                        raise RuntimeError(
                            f"Captcha rejected {captcha_attempts} times. If the answers were "
                            "correct, your IP is likely blocked or rate-limited by Keep2Share."
                        )
                    captcha_attempts += 1
                    _emit_status(
                        status_callback,
                        f"Captcha invalid, requesting a new one "
                        f"(attempt {captcha_attempts}/{MAX_CAPTCHA_ATTEMPTS}).",
                    )
                    captcha = fetch_captcha(status_callback)
                    captcha_image = requests.get(captcha["captcha_url"], timeout=DEFAULT_TIMEOUT).content
                    response = captcha_callback(captcha_image, captcha["challenge"], captcha["captcha_url"])
                    continue
                if message == "File not found":
                    raise K2SFileNotFound(
                        "File not found on Keep2Share (it may have been removed or the link is wrong)."
                    )

            if "time_wait" not in free_r:
                free_download_key = free_r.get("free_download_key", "")
                working_link = True
                break

            wait_time = int(free_r["time_wait"])
            if wait_time > 30:
                break

            for remaining in range(wait_time - 1, 0, -1):
                _raise_if_cancelled(stop_event)
                _emit_status(status_callback, f"[{label}] Waiting {remaining} seconds...")
                time.sleep(1)

            free_download_key = free_r["free_download_key"]
            working_link = True

        if not working_link:
            continue

        session = FuturesSession(max_workers=5)

        rounds_without_progress = 0
        while len(urls) < count:
            _raise_if_cancelled(stop_event)
            futures = []
            to_generate = count - len(urls)
            for _ in range(to_generate):
                future = session.post(
                    f"https://{choice(DOMAINS)}/api/v2/getUrl",
                    json={"file_id": file_id, "free_download_key": free_download_key},
                    proxies=prox,
                    timeout=DEFAULT_TIMEOUT,
                )
                futures.append(future)

            iterator = as_completed(futures)
            iterator = tqdm(iterator, total=len(futures), leave=False, disable=status_callback is not None)

            gained = 0
            for future in iterator:
                try:
                    result = future.result()
                    urls.append(result.json()["url"])
                    gained += 1
                except KeyboardInterrupt:
                    raise
                except Exception:
                    continue

            if gained == 0:
                rounds_without_progress += 1
                if rounds_without_progress >= MAX_URL_BATCH_ROUNDS:
                    _emit_status(
                        status_callback,
                        f"[{label}] No new download URLs after {MAX_URL_BATCH_ROUNDS} rounds; "
                        "this proxy/IP is likely blocked.",
                    )
                    break
            else:
                rounds_without_progress = 0

        if not urls:
            _emit_status(status_callback, f"[{label}] Could not generate any download URLs; trying next proxy.")
            working_link = False
            free_download_key = ""
            continue

        break

    if not urls:
        raise RuntimeError(
            "No working download URLs could be generated. Your IP and every proxy tried appear "
            "to be blocked or rate-limited by Keep2Share. Try refreshing the proxy list, waiting "
            "a while before retrying, or -- if you're on a dynamic IP -- restarting your router/modem "
            "to get a new one."
        )

    if len(urls) < count:
        _emit_status(
            status_callback,
            f"Only generated {len(urls)}/{count} download URLs; continuing with fewer connections.",
        )

    return urls[:count]


def get_name(file_id: str) -> str:
    response = requests.post(
        f"https://{choice(DOMAINS)}/api/v2/getFilesInfo",
        json={"ids": [file_id]},
        timeout=DEFAULT_TIMEOUT,
    ).json()
    return response["files"][0]["name"]
