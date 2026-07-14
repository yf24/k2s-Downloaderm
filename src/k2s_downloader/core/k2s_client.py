from __future__ import annotations

import sys
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
) -> List[str]:
    """Collect temporary download URLs for the given file identifier."""

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

    captcha = fetch_captcha(status_callback)
    captcha_image = requests.get(captcha["captcha_url"], timeout=DEFAULT_TIMEOUT).content
    response = captcha_callback(captcha_image, captcha["challenge"], captcha["captcha_url"])

    for proxy in proxy_pool:
        label = proxy or "LOCAL"
        _emit_status(status_callback, f"Trying proxy {label}")
        prox = {"https": f"http://{proxy}"} if proxy else None

        while not working_link:
            try:
                free_r = requests.post(
                    f"https://{choice(DOMAINS)}/api/v2/getUrl",
                    json={
                        "file_id": file_id,
                        "captcha_challenge": captcha["challenge"],
                        "captcha_response": response,
                    },
                    proxies=prox,
                    timeout=5,
                ).json()
            except KeyboardInterrupt:
                raise
            except Exception:
                break

            if free_r.get("status") == "error":
                message = free_r.get("message", "")
                if message == "Invalid captcha code":
                    _emit_status(status_callback, "Captcha invalid, requesting a new one.")
                    captcha = fetch_captcha(status_callback)
                    captcha_image = requests.get(captcha["captcha_url"], timeout=DEFAULT_TIMEOUT).content
                    response = captcha_callback(captcha_image, captcha["challenge"], captcha["captcha_url"])
                    continue
                if message == "File not found":
                    sys.exit("File not found")

            if "time_wait" not in free_r:
                free_download_key = free_r.get("free_download_key", "")
                working_link = True
                break

            wait_time = int(free_r["time_wait"])
            if wait_time > 30:
                break

            for remaining in range(wait_time - 1, 0, -1):
                _emit_status(status_callback, f"[{label}] Waiting {remaining} seconds...")
                time.sleep(1)

            free_download_key = free_r["free_download_key"]
            working_link = True

        if not working_link:
            continue

        session = FuturesSession(max_workers=5)

        while len(urls) < count:
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

            for future in iterator:
                try:
                    result = future.result()
                    urls.append(result.json()["url"])
                except KeyboardInterrupt:
                    raise
                except Exception:
                    continue

        break

    if not urls:
        raise RuntimeError("No working links found")

    return urls[:count]


def get_name(file_id: str) -> str:
    response = requests.post(
        f"https://{choice(DOMAINS)}/api/v2/getFilesInfo",
        json={"ids": [file_id]},
        timeout=DEFAULT_TIMEOUT,
    ).json()
    return response["files"][0]["name"]
