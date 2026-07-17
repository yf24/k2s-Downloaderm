"""Tests for P2-3 (prefer direct connection over public proxies) and P2-4
(configurable proxy cache path instead of a hardcoded CWD file).

P2-3: public proxies are untrusted (see the security note in proxy.py /
Readme.md), so ``Downloader._acquire_proxy_lock`` should always try the
direct connection (index 0, which ``get_working_proxies`` guarantees is
always ``None``) before falling back to a third-party proxy.

P2-4: ``get_working_proxies`` previously hardcoded ``proxies.txt`` in the
current working directory with no way to override it, polluting whatever
directory the process happened to run from. It now accepts a ``cache_path``
parameter, and ``Downloader`` threads its own ``proxy_cache_path`` through
to it.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from k2s_downloader.core.downloader import Downloader
from k2s_downloader.core.proxy import get_working_proxies


class TestDirectConnectionPreference:
    def _downloader(self, proxy_count: int) -> Downloader:
        downloader = Downloader()
        downloader.proxies = [None] + [f"1.2.3.{i}:8080" for i in range(proxy_count - 1)]
        downloader.proxy_locks = [threading.Lock() for _ in range(proxy_count)]
        downloader.working_proxy_indexes = []
        return downloader

    def test_prefers_direct_connection_when_free(self):
        downloader = self._downloader(4)
        idx = downloader._acquire_proxy_lock()
        assert idx == 0
        downloader.proxy_locks[0].release()

    def test_prefers_direct_connection_on_every_call_once_released(self):
        downloader = self._downloader(3)
        for _ in range(5):
            idx = downloader._acquire_proxy_lock()
            assert idx == 0
            downloader.proxy_locks[0].release()

    def test_falls_back_to_a_proxy_when_direct_is_already_in_use(self):
        downloader = self._downloader(4)
        downloader.proxy_locks[0].acquire()  # simulate direct already busy
        try:
            idx = downloader._acquire_proxy_lock()
            assert idx is not None
            assert idx != 0
            downloader.proxy_locks[idx].release()
        finally:
            downloader.proxy_locks[0].release()


class TestProxyCachePathConfigurable:
    def test_reads_cached_proxies_from_custom_path(self, tmp_path):
        custom_path = tmp_path / "custom_proxies.txt"
        custom_path.write_text("1.2.3.4:8080\n5.6.7.8:8080")

        result = get_working_proxies(cache_path=custom_path)

        assert result == [None, "1.2.3.4:8080", "5.6.7.8:8080"]

    def test_writes_working_proxies_to_custom_path_on_success(self, tmp_path):
        custom_path = tmp_path / "proxies.txt"

        proxyscrape_response = MagicMock()
        proxyscrape_response.text = "1.2.3.4:8080"

        fake_future = MagicMock()
        fake_future.result.return_value = MagicMock(status_code=200)
        fake_session = MagicMock()
        fake_session.head.return_value = fake_future

        with patch("k2s_downloader.core.proxy.requests.get", return_value=proxyscrape_response), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=fake_session), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(refresh=True, cache_path=custom_path, status_callback=lambda _m: None)

        assert result == [None, "1.2.3.4:8080"]
        assert custom_path.read_text() == "1.2.3.4:8080"

    def test_creates_missing_parent_directories_for_nested_cache_path(self, tmp_path):
        nested_path = tmp_path / "nested" / "dir" / "proxies.txt"
        assert not nested_path.parent.exists()

        proxyscrape_response = MagicMock()
        proxyscrape_response.text = "1.2.3.4:8080"

        # Every candidate fails HTTPS validation, exercising the
        # "No proxies passed HTTPS validation" write-empty-cache path.
        fake_future = MagicMock()
        fake_future.result.side_effect = Exception("simulated failure")
        fake_session = MagicMock()
        fake_session.head.return_value = fake_future

        with patch("k2s_downloader.core.proxy.requests.get", return_value=proxyscrape_response), \
             patch("k2s_downloader.core.proxy.FuturesSession", return_value=fake_session), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(refresh=True, cache_path=nested_path, status_callback=lambda _m: None)

        assert result == [None]
        assert nested_path.exists()
        assert nested_path.read_text() == ""

    def test_recheck_cached_revalidates_and_drops_dead_proxies(self, tmp_path):
        custom_path = tmp_path / "proxies.txt"
        custom_path.write_text("1.2.3.4:8080\n5.6.7.8:8080")

        good_future = MagicMock()
        good_future.result.return_value = MagicMock(status_code=200)
        dead_future = MagicMock()
        dead_future.result.side_effect = Exception("dead proxy")

        def fake_head(url, *, proxies, timeout):
            proxy_value = proxies["https"].removeprefix("http://")
            return good_future if proxy_value == "1.2.3.4:8080" else dead_future

        fake_session = MagicMock()
        fake_session.head.side_effect = fake_head

        with patch("k2s_downloader.core.proxy.FuturesSession", return_value=fake_session), \
             patch("k2s_downloader.core.proxy.as_completed", side_effect=lambda futures: list(futures)), \
             patch("k2s_downloader.core.proxy.time.sleep"):
            result = get_working_proxies(recheck_cached=True, cache_path=custom_path, status_callback=lambda _m: None)

        assert result == [None, "1.2.3.4:8080"]
        assert custom_path.read_text() == "1.2.3.4:8080"


class TestDownloaderProxyCachePathPassthrough:
    def test_refresh_proxies_passes_configured_cache_path_through(self, tmp_path):
        custom_path = tmp_path / "custom" / "proxies.txt"
        downloader = Downloader(proxy_cache_path=custom_path)

        with patch("k2s_downloader.core.downloader.get_working_proxies") as mock_get:
            mock_get.return_value = [None]
            downloader.refresh_proxies()

        _, kwargs = mock_get.call_args
        assert kwargs["cache_path"] == custom_path

    def test_default_proxy_cache_path_matches_get_working_proxies_default(self):
        # Keep Downloader's default in sync with get_working_proxies' own
        # default so behaviour is unchanged for callers that don't override
        # either.
        downloader = Downloader()
        assert str(downloader.proxy_cache_path) == "proxies.txt"
