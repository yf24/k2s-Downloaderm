"""Tests for R2-10 items 1-3: proxy pool source/validation/lifecycle quality.

- Item 1: candidates are aggregated from several independently-maintained
  sources instead of a single now-deprecated proxyscrape v1 endpoint, so one
  source going down/changing format doesn't silently degrade the whole app
  to direct-connection-only.
- Item 2: validation is against the actual download target (k2s.cc), not a
  generic reachability check (api.myip.com) -- and a non-2xx/3xx response is
  now treated as a validation failure, not just a raised exception.
- Item 3: a cached proxy list older than PROXY_CACHE_TTL_SECONDS is treated
  as stale and revalidated instead of being returned as-is forever.
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import requests

from k2s_downloader.core import proxy as proxy_module
from k2s_downloader.core.proxy import get_working_proxies


def _passing_session():
    """A FuturesSession stand-in whose .head() always "succeeds" (200).

    Each call gets its own future (not a shared one) -- the real validation
    loop stashes the requested proxy on ``future.proxy`` per call, which
    would clobber a single shared mock's attribute across candidates.
    """
    def make_future(*args, **kwargs):
        future = MagicMock()
        future.result.return_value = MagicMock(status_code=200)
        return future

    session = MagicMock()
    session.head.side_effect = make_future
    return session


class TestMultiSourceAggregation:
    def test_candidates_from_multiple_sources_are_merged(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"

        def fake_get(url, *, timeout):
            if "proxyscrape" in url:
                return MagicMock(text="1.1.1.1:80")
            if "TheSpeedX" in url:
                return MagicMock(text="2.2.2.2:80")
            if "monosans" in url:
                return MagicMock(text="3.3.3.3:80")
            if "proxifly" in url:
                return MagicMock(text="4.4.4.4:80")
            raise AssertionError(f"unexpected source url: {url}")

        with patch("k2s_downloader.core.proxy.requests.get", side_effect=fake_get), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=_passing_session()), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(refresh=True, cache_path=cache_path, status_callback=lambda _m: None)

        assert set(result) == {None, "1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80", "4.4.4.4:80"}

    def test_one_source_failing_does_not_block_the_others(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"
        messages: list[str] = []

        def fake_get(url, *, timeout):
            if "proxyscrape" in url:
                raise requests.exceptions.ConnectionError("proxyscrape is down")
            return MagicMock(text="2.2.2.2:80")

        with patch("k2s_downloader.core.proxy.requests.get", side_effect=fake_get), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=_passing_session()), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(refresh=True, cache_path=cache_path, status_callback=messages.append)

        assert "2.2.2.2:80" in result
        assert any("proxyscrape" in msg and "unavailable" in msg for msg in messages)

    def test_scheme_prefixed_candidates_are_normalized(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"

        with patch(
            "k2s_downloader.core.proxy.requests.get",
            return_value=MagicMock(text="http://5.5.5.5:8080"),
        ), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=_passing_session()), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(refresh=True, cache_path=cache_path, status_callback=lambda _m: None)

        assert "5.5.5.5:8080" in result
        assert "http://5.5.5.5:8080" not in result


class TestValidationTargetsK2S:
    def test_validation_request_targets_k2s_not_a_generic_reachability_check(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"
        session = _passing_session()

        with patch(
            "k2s_downloader.core.proxy.requests.get",
            return_value=MagicMock(text="1.2.3.4:8080"),
        ), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=session), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            get_working_proxies(refresh=True, cache_path=cache_path, status_callback=lambda _m: None)

        _, kwargs = session.head.call_args
        args, _ = session.head.call_args
        called_url = args[0] if args else kwargs.get("url")
        assert called_url == proxy_module.PROXY_VALIDATION_URL
        assert "myip.com" not in called_url

    def test_proxy_reachable_but_blocked_by_target_is_rejected(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"
        blocked_future = MagicMock()
        blocked_future.result.return_value = MagicMock(status_code=403)
        session = MagicMock()
        session.head.return_value = blocked_future

        with patch(
            "k2s_downloader.core.proxy.requests.get",
            return_value=MagicMock(text="1.2.3.4:8080"),
        ), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=session), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(refresh=True, cache_path=cache_path, status_callback=lambda _m: None)

        # Connected fine (no exception) but the target itself rejected it --
        # must not be treated as a usable proxy.
        assert result == [None]

    def test_proxy_allowed_through_by_target_passes(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"

        with patch(
            "k2s_downloader.core.proxy.requests.get",
            return_value=MagicMock(text="1.2.3.4:8080"),
        ), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=_passing_session()), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(refresh=True, cache_path=cache_path, status_callback=lambda _m: None)

        assert result == [None, "1.2.3.4:8080"]


class TestCacheTTL:
    def test_fresh_cache_is_returned_without_any_network_activity(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"
        cache_path.write_text("1.2.3.4:8080")

        with patch("k2s_downloader.core.proxy.requests.get") as mock_get, \
             patch("k2s_downloader.core.proxy.FuturesSession") as mock_session_cls:
            result = get_working_proxies(cache_path=cache_path)

        assert result == [None, "1.2.3.4:8080"]
        mock_get.assert_not_called()
        mock_session_cls.assert_not_called()

    def test_stale_cache_is_revalidated_instead_of_returned_as_is(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"
        cache_path.write_text("1.2.3.4:8080")
        stale_time = time.time() - proxy_module.PROXY_CACHE_TTL_SECONDS - 60
        os.utime(cache_path, (stale_time, stale_time))

        with patch("k2s_downloader.core.proxy.requests.get") as mock_get, \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=_passing_session()), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(cache_path=cache_path)

        # Stale cache -> auto-promoted to a recheck (revalidate what's
        # cached), not a full re-fetch from every source.
        mock_get.assert_not_called()
        assert result == [None, "1.2.3.4:8080"]

    def test_stale_and_dead_cached_proxy_is_dropped(self, tmp_path):
        cache_path = tmp_path / "proxies.txt"
        cache_path.write_text("1.2.3.4:8080")
        stale_time = time.time() - proxy_module.PROXY_CACHE_TTL_SECONDS - 60
        os.utime(cache_path, (stale_time, stale_time))

        dead_future = MagicMock()
        dead_future.result.side_effect = Exception("dead")
        session = MagicMock()
        session.head.return_value = dead_future

        with patch("k2s_downloader.core.proxy.requests.get") as mock_get, \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=session), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(cache_path=cache_path)

        mock_get.assert_not_called()
        assert result == [None]
