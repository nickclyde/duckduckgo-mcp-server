import asyncio
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, patch, MagicMock
import unittest

import httpx
from starlette.routing import Route as StarletteRoute

import duckduckgo_mcp_server.server
from duckduckgo_mcp_server.server import _build_transport_security

from duckduckgo_mcp_server.server import (
    RateLimiter,
    TokenBucketLimiter,
    HostRateLimiter,
    TTLCache,
    DuckDuckGoSearcher,
    SafeSearchMode,
    SearchResult,
    SUPPORTED_FETCH_BACKENDS,
    SUPPORTED_RATE_STRATEGIES,
    WebContentFetcher,
    BlockedURLError,
    _validate_public_url,
    _is_search_block,
    _resolve_ssl_verify,
    _retry_after_seconds,
    make_rate_limiter,
    LinkRegistry,
    is_ref_token,
    DEFAULT_REF_URL_THRESHOLD,
    _normalize_cache_url,
    _content_cache_key,
    _html_to_text,
    _env_int,
    SUPPORTED_PARSE_MODES,
    _safe_markdown_href,
)

try:
    import curl_cffi  # noqa: F401
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class DummyCtx:
    async def info(self, message):
        return None

    async def error(self, message):
        return None


class TestRateLimiter(unittest.TestCase):
    def test_acquire_removes_expired_entries(self):
        limiter = RateLimiter(requests_per_minute=1)
        limiter.requests.append(datetime.now() - timedelta(minutes=2))

        asyncio.run(limiter.acquire())

        self.assertEqual(len(limiter.requests), 1)
        self.assertLess((datetime.now() - limiter.requests[0]).total_seconds(), 1.0)


class TestRateLimiterEdgeCases(unittest.TestCase):
    def test_acquire_blocks_when_at_capacity(self):
        limiter = RateLimiter(requests_per_minute=2)
        now = datetime.now()
        limiter.requests = [now - timedelta(seconds=10), now - timedelta(seconds=5)]

        async def fake_sleep(seconds):
            # Advance the window the same way a real wait would.
            limiter.requests = [
                t - timedelta(seconds=seconds + 0.1) for t in limiter.requests
            ]

        with patch("asyncio.sleep", side_effect=fake_sleep) as mock_sleep:
            asyncio.run(limiter.acquire())
            mock_sleep.assert_called()
            wait_time = mock_sleep.call_args_list[0][0][0]
            self.assertGreater(wait_time, 40)
            self.assertLessEqual(wait_time, 60)
            # Recorded after the wait, so we stay at the cap instead of rpm+1.
            self.assertEqual(len(limiter.requests), 2)

    def test_acquire_allows_after_window_expires(self):
        limiter = RateLimiter(requests_per_minute=2)
        limiter.requests = [
            datetime.now() - timedelta(seconds=61),
            datetime.now() - timedelta(seconds=61),
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(limiter.acquire())
            mock_sleep.assert_not_called()


class TestTokenBucketAndHostLimits(unittest.TestCase):
    def test_make_rate_limiter_strategies(self):
        self.assertEqual(SUPPORTED_RATE_STRATEGIES, ("sliding", "token_bucket"))
        self.assertIsInstance(make_rate_limiter("sliding", 10), RateLimiter)
        self.assertIsInstance(make_rate_limiter("token_bucket", 10), TokenBucketLimiter)
        with self.assertRaises(ValueError):
            make_rate_limiter("bogus", 10)

    def test_token_bucket_allows_burst_without_sleep(self):
        limiter = TokenBucketLimiter(requests_per_minute=30, burst=2)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(limiter.acquire())
            asyncio.run(limiter.acquire())
            mock_sleep.assert_not_called()

    def test_token_bucket_sleeps_when_empty(self):
        limiter = TokenBucketLimiter(requests_per_minute=30, burst=1)
        asyncio.run(limiter.acquire())
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(limiter.acquire())
            mock_sleep.assert_called_once()
            self.assertGreater(mock_sleep.call_args[0][0], 0)

    def test_host_limiter_isolates_hosts(self):
        limiter = HostRateLimiter("sliding", requests_per_minute=1)

        async def fake_sleep(seconds):
            # Age past the 60s window so wait-then-record can take a slot.
            extra = max(seconds, 0) + 0.1
            for child in limiter._limiters.values():
                if hasattr(child, "requests"):
                    child.requests = [t - timedelta(seconds=extra) for t in child.requests]

        with patch("asyncio.sleep", side_effect=fake_sleep) as mock_sleep:
            asyncio.run(limiter.acquire("https://a.example/1"))
            asyncio.run(limiter.acquire("https://b.example/1"))
            mock_sleep.assert_not_called()
            asyncio.run(limiter.acquire("https://a.example/2"))
            mock_sleep.assert_called()

    def test_host_limiter_evicts_idle_hosts(self):
        limiter = HostRateLimiter("sliding", requests_per_minute=5)
        asyncio.run(limiter.acquire("https://a.example/1"))
        asyncio.run(limiter.acquire("https://b.example/1"))
        self.assertEqual(set(limiter._limiters), {"a.example", "b.example"})
        # Age a.example's only request out of the window; the next acquire prunes it.
        limiter._limiters["a.example"].requests = [datetime.now() - timedelta(seconds=61)]
        asyncio.run(limiter.acquire("https://c.example/1"))
        self.assertNotIn("a.example", limiter._limiters)
        self.assertIn("b.example", limiter._limiters)
        self.assertIn("c.example", limiter._limiters)

    def test_token_bucket_idle_after_refill(self):
        limiter = TokenBucketLimiter(requests_per_minute=60, burst=1)
        asyncio.run(limiter.acquire())
        self.assertFalse(limiter.idle())
        limiter.updated -= 5  # pretend 5s passed: refills the single-token bucket
        self.assertTrue(limiter.idle())

    def test_fetcher_host_limiter_off_by_default(self):
        self.assertIsNone(WebContentFetcher().host_limiter)
        self.assertIsNotNone(WebContentFetcher(host_requests_per_minute=5).host_limiter)

    def test_retry_after_seconds(self):
        self.assertEqual(_retry_after_seconds({"retry-after": "5"}), 5.0)
        self.assertIsNone(_retry_after_seconds({"retry-after": "Fri, 01 Jan 2030"}))
        self.assertIsNone(_retry_after_seconds({}))

    def test_search_retries_once_on_429(self):
        searcher = DuckDuckGoSearcher(backend="httpx")
        html = "<html><body></body></html>"
        blocked = MagicMock(spec=httpx.Response)
        blocked.status_code = 429
        blocked.headers = {"retry-after": "1"}
        blocked.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("429", request=MagicMock(), response=blocked)
        )
        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        ok.text = html
        ok.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[blocked, ok])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            status, body = asyncio.run(searcher._request_httpx({"q": "x"}))

        self.assertEqual(status, 200)
        self.assertEqual(body, html)
        self.assertEqual(mock_client.post.call_count, 2)
        mock_sleep.assert_called_once()

    def test_main_parses_rate_limit_flags(self):
        with patch.object(
            sys,
            "argv",
            [
                "duckduckgo-mcp-server",
                "--rate-limit-strategy",
                "token_bucket",
                "--search-rpm",
                "12",
                "--fetch-rpm",
                "8",
                "--fetch-host-rpm",
                "0",
            ],
        ), patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertIsInstance(
            duckduckgo_mcp_server.server.searcher.rate_limiter, TokenBucketLimiter
        )
        self.assertEqual(
            duckduckgo_mcp_server.server.searcher.rate_limiter.requests_per_minute, 12
        )
        self.assertEqual(
            duckduckgo_mcp_server.server.fetcher.rate_limiter.requests_per_minute, 8
        )
        self.assertIsNone(duckduckgo_mcp_server.server.fetcher.host_limiter)


class TestTTLCache(unittest.TestCase):
    def test_get_returns_none_when_empty(self):
        cache = TTLCache(ttl_seconds=60, max_entries=8)
        self.assertIsNone(cache.get("missing"))

    def test_round_trip(self):
        cache = TTLCache(ttl_seconds=60, max_entries=8)
        cache.set("k", "v")
        self.assertEqual(cache.get("k"), "v")

    def test_expired_entry_is_a_miss(self):
        cache = TTLCache(ttl_seconds=10, max_entries=8)
        cache.set("k", "v")
        with patch("duckduckgo_mcp_server.server.time.monotonic", return_value=time.monotonic() + 11):
            self.assertIsNone(cache.get("k"))
        self.assertEqual(len(cache), 0)

    def test_zero_ttl_disables_cache(self):
        cache = TTLCache(ttl_seconds=0, max_entries=8)
        self.assertFalse(cache.enabled)
        cache.set("k", "v")
        self.assertIsNone(cache.get("k"))

    def test_zero_max_entries_disables_cache(self):
        cache = TTLCache(ttl_seconds=60, max_entries=0)
        self.assertFalse(cache.enabled)
        cache.set("k", "v")
        self.assertIsNone(cache.get("k"))

    def test_lru_evicts_oldest(self):
        cache = TTLCache(ttl_seconds=60, max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        self.assertEqual(cache.get("c"), 3)

    def test_get_refreshes_lru_order(self):
        cache = TTLCache(ttl_seconds=60, max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        self.assertEqual(cache.get("a"), 1)  # a becomes most recently used
        cache.set("c", 3)
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), 3)

    def test_normalize_cache_url_drops_fragment_and_default_port(self):
        self.assertEqual(
            _normalize_cache_url("HTTPS://Example.COM:443/path#frag"),
            "https://example.com/path",
        )
        self.assertEqual(
            _normalize_cache_url("http://example.com:8080/x?q=1"),
            "http://example.com:8080/x?q=1",
        )

    def test_content_cache_key_includes_backend(self):
        key = _content_cache_key("https://Example.com/a#x", "httpx")
        self.assertEqual(key, ("https://example.com/a", "httpx", "text"))
        self.assertEqual(
            _content_cache_key("https://Example.com/a#x", "httpx", "markdown"),
            ("https://example.com/a", "httpx", "markdown"),
        )

    def test_html_to_text_strips_chrome(self):
        html = (
            "<html><body><nav>Nav</nav><h1>Title</h1>"
            "<script>alert(1)</script><p>Body</p><footer>Foot</footer></body></html>"
        )
        text = _html_to_text(html)
        self.assertIn("Title", text)
        self.assertIn("Body", text)
        self.assertNotIn("Nav", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("Foot", text)

    def test_env_int_defaults_on_bad_input(self):
        with patch.dict(os.environ, {"DDG_CACHE_TTL": "nope"}, clear=False):
            self.assertEqual(_env_int("DDG_CACHE_TTL", 300), 300)
        with patch.dict(os.environ, {"DDG_CACHE_TTL": "-5"}, clear=False):
            self.assertEqual(_env_int("DDG_CACHE_TTL", 300), 300)
        with patch.dict(os.environ, {"DDG_CACHE_TTL": "12"}, clear=False):
            self.assertEqual(_env_int("DDG_CACHE_TTL", 300), 12)


_LONG_URL = "https://example.com/articles/2026/09/04/" + "a-very-long-slug-" * 8 + "?utm_source=x&utm_medium=y"


class TestLinkRegistry(unittest.TestCase):
    def test_shorten_is_stable_and_round_trips(self):
        reg = LinkRegistry()
        token = reg.shorten(_LONG_URL)
        self.assertTrue(token.startswith("ref://"))
        self.assertEqual(len(token), len("ref://") + 8)
        self.assertEqual(reg.shorten(_LONG_URL), token)
        self.assertEqual(len(reg), 1)
        self.assertEqual(reg.resolve(token), _LONG_URL)
        # Bare id, mixed case, and a trailing slash all resolve.
        bare = token[len("ref://"):]
        self.assertEqual(reg.resolve(bare), _LONG_URL)
        self.assertEqual(reg.resolve("REF://" + bare.upper() + "/"), _LONG_URL)

    def test_resolve_unknown_returns_none(self):
        reg = LinkRegistry()
        self.assertIsNone(reg.resolve("ref://deadbeef"))
        self.assertIsNone(reg.resolve(""))
        self.assertIsNone(reg.resolve("https://example.com"))

    def test_collision_extends_id(self):
        reg = LinkRegistry()
        token = reg.shorten(_LONG_URL)
        key = token[len("ref://"):]
        # Simulate another URL already owning the 8-char prefix.
        reg._urls.clear()
        reg._urls[key] = "https://other.example/"
        longer = reg.shorten(_LONG_URL)
        self.assertNotEqual(longer, token)
        self.assertTrue(longer[len("ref://"):].startswith(key))
        self.assertEqual(reg.resolve(longer), _LONG_URL)
        self.assertEqual(reg.resolve(token), "https://other.example/")

    def test_lru_eviction(self):
        reg = LinkRegistry(max_entries=2)
        t1 = reg.shorten("https://one.example/" + "x" * 50)
        t2 = reg.shorten("https://two.example/" + "x" * 50)
        reg.resolve(t1)  # t1 becomes most recently used
        reg.shorten("https://three.example/" + "x" * 50)
        self.assertIsNotNone(reg.resolve(t1))
        self.assertIsNone(reg.resolve(t2))
        self.assertEqual(len(reg), 2)

    def test_is_ref_token(self):
        self.assertTrue(is_ref_token("ref://abc"))
        self.assertTrue(is_ref_token("  REF://abc"))
        self.assertFalse(is_ref_token("https://example.com"))
        self.assertFalse(is_ref_token(""))


class TestRefLinksInToolOutput(unittest.TestCase):
    def test_format_results_shortens_only_long_urls(self):
        reg = LinkRegistry()
        searcher = DuckDuckGoSearcher(ref_url_threshold=60, link_registry=reg)
        results = [
            SearchResult(title="Short", link="https://example.com/a", snippet="s", position=1),
            SearchResult(title="Long", link=_LONG_URL, snippet="l", position=2),
        ]
        out = searcher.format_results_for_llm(results)
        self.assertIn("URL: https://example.com/a", out)
        self.assertNotIn(_LONG_URL, out)
        self.assertIn("URL: ref://", out)
        self.assertIn("expand_link", out)
        token = next(w for w in out.split() if w.startswith("ref://"))
        self.assertEqual(reg.resolve(token), _LONG_URL)

    def test_default_threshold_and_disable(self):
        self.assertEqual(DuckDuckGoSearcher().ref_url_threshold, DEFAULT_REF_URL_THRESHOLD)
        reg = LinkRegistry()
        searcher = DuckDuckGoSearcher(ref_url_threshold=0, link_registry=reg)
        out = searcher.format_results_for_llm(
            [SearchResult(title="Long", link=_LONG_URL, snippet="l", position=1)]
        )
        self.assertIn(_LONG_URL, out)
        self.assertEqual(len(reg), 0)

    def test_fetch_and_parse_resolves_ref_token(self):
        reg = LinkRegistry()
        token = reg.shorten(_LONG_URL)
        fetcher = WebContentFetcher(backend="httpx", link_registry=reg)
        seen = {}

        async def fake_httpx(url):
            seen["url"] = url
            return "<html><body><p>Resolved page</p></body></html>"

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx):
            result = asyncio.run(fetcher.fetch_and_parse(token, DummyCtx()))

        self.assertEqual(seen["url"], _LONG_URL)
        self.assertIn("Resolved page", result)

    def test_fetch_and_parse_unknown_ref_token_does_not_fetch(self):
        fetcher = WebContentFetcher(backend="httpx", link_registry=LinkRegistry())
        with patch.object(fetcher, "_fetch_httpx", new_callable=AsyncMock) as mock_fetch:
            result = asyncio.run(fetcher.fetch_and_parse("ref://deadbeef", DummyCtx()))
        mock_fetch.assert_not_called()
        self.assertTrue(result.startswith("Error: Unknown link reference 'ref://deadbeef'"))

    def test_main_parses_ref_url_threshold_flag(self):
        with patch.object(sys, "argv", ["duckduckgo-mcp-server", "--ref-url-threshold", "0"]), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertEqual(duckduckgo_mcp_server.server.searcher.ref_url_threshold, 0)
        with patch.object(sys, "argv", ["duckduckgo-mcp-server", "--ref-url-threshold", "-1"]), \
             patch("duckduckgo_mcp_server.server.mcp"):
            with self.assertRaises(SystemExit):
                duckduckgo_mcp_server.server.main()


class TestDuckDuckGoSearcher(unittest.TestCase):
    def test_format_results_for_llm_populates_entries(self):
        searcher = DuckDuckGoSearcher()
        results = [
            SearchResult(
                title="First Result",
                link="https://example.com/first",
                snippet="Snippet one",
                position=1,
            ),
            SearchResult(
                title="Second Result",
                link="https://example.com/second",
                snippet="Snippet two",
                position=2,
            ),
        ]

        formatted = searcher.format_results_for_llm(results)

        self.assertIn("Found 2 search results", formatted)
        self.assertIn("1. First Result", formatted)
        self.assertIn("URL: https://example.com/first", formatted)

    def test_format_results_for_llm_handles_empty(self):
        searcher = DuckDuckGoSearcher()

        formatted = searcher.format_results_for_llm([])

        self.assertIn("No results were found", formatted)


def _make_ddg_html(results):
    """Build a minimal DDG-like HTML page with the given result dicts."""
    items = []
    for r in results:
        snippet_html = ""
        if r.get("snippet"):
            snippet_html = f'<a class="result__snippet">{r["snippet"]}</a>'
        items.append(
            f'<div class="result">'
            f'  <h2 class="result__title"><a href="{r["href"]}">{r["title"]}</a></h2>'
            f"  {snippet_html}"
            f"</div>"
        )
    return f"<html><body>{''.join(items)}</body></html>"


def _mock_post_response(html, status_code=200):
    """Create a mock httpx.Response for POST requests."""
    resp = MagicMock(spec=httpx.Response)
    resp.text = html
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


class TestDuckDuckGoSearcherParsing(unittest.TestCase):
    def _run_search(self, html, max_results=10, region=""):
        """Helper to run a search with mocked HTTP."""
        searcher = DuckDuckGoSearcher()
        ctx = DummyCtx()

        mock_resp = _mock_post_response(html)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            results = asyncio.run(searcher.search("test query", ctx, max_results, region))
        return results

    def test_search_parses_results_from_html(self):
        html = _make_ddg_html([
            {"title": "Result One", "href": "https://one.com", "snippet": "Snippet 1"},
            {"title": "Result Two", "href": "https://two.com", "snippet": "Snippet 2"},
            {"title": "Result Three", "href": "https://three.com", "snippet": "Snippet 3"},
        ])
        results = self._run_search(html)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].title, "Result One")
        self.assertEqual(results[0].link, "https://one.com")
        self.assertEqual(results[0].snippet, "Snippet 1")
        self.assertEqual(results[1].title, "Result Two")
        self.assertEqual(results[2].title, "Result Three")

    def test_search_cleans_redirect_urls(self):
        encoded_url = "https%3A%2F%2Fexample.com%2Fpage"
        html = _make_ddg_html([
            {
                "title": "Redirected",
                "href": f"//duckduckgo.com/l/?uddg={encoded_url}&rut=abc",
                "snippet": "A snippet",
            },
        ])
        results = self._run_search(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].link, "https://example.com/page")

    def test_search_filters_ads(self):
        html = _make_ddg_html([
            {"title": "Ad Result", "href": "https://duckduckgo.com/y.js?ad=1", "snippet": "Ad"},
            {"title": "Real Result", "href": "https://real.com", "snippet": "Real"},
        ])
        results = self._run_search(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Real Result")

    def test_search_respects_max_results(self):
        html = _make_ddg_html([
            {"title": f"R{i}", "href": f"https://r{i}.com", "snippet": f"S{i}"}
            for i in range(5)
        ])
        results = self._run_search(html, max_results=2)
        self.assertEqual(len(results), 2)

    def test_search_handles_missing_snippet(self):
        html = _make_ddg_html([
            {"title": "No Snippet", "href": "https://nosnip.com"},
        ])
        results = self._run_search(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "")

    def test_search_returns_empty_on_timeout(self):
        searcher = DuckDuckGoSearcher()
        ctx = DummyCtx()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            results = asyncio.run(searcher.search("test", ctx))
        self.assertEqual(results, [])

    def test_search_returns_empty_on_http_error(self):
        searcher = DuckDuckGoSearcher()
        ctx = DummyCtx()

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.request = MagicMock()
        error = httpx.HTTPStatusError("error", request=mock_resp.request, response=mock_resp)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_resp.raise_for_status = MagicMock(side_effect=error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            results = asyncio.run(searcher.search("test", ctx))
        self.assertEqual(results, [])

    def test_search_returns_empty_on_no_results(self):
        html = "<html><body><p>No results</p></body></html>"
        results = self._run_search(html)
        self.assertEqual(results, [])


class TestDuckDuckGoSearcherBackend(unittest.TestCase):
    def test_is_search_block_truth_table(self):
        # 202 (fingerprint block) and 403 are block signals regardless of body.
        self.assertTrue(_is_search_block(202, "<html>14kb block page</html>"))
        self.assertTrue(_is_search_block(403, "forbidden"))
        # A truly empty 200 body is a block; a 200 with any content is not.
        self.assertTrue(_is_search_block(200, "   "))
        self.assertFalse(_is_search_block(200, "<html>real results</html>"))
        # Non-2xx errors are handled via raise_for_status, not this helper.
        self.assertFalse(_is_search_block(500, ""))

    def test_default_backend_is_auto(self):
        self.assertEqual(DuckDuckGoSearcher().backend, "auto")

    def test_init_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            DuckDuckGoSearcher(backend="bogus")

    def test_auto_falls_back_to_curl_on_202(self):
        """A 202 fingerprint-block on httpx must transparently retry with curl."""
        searcher = DuckDuckGoSearcher(backend="auto")
        html = _make_ddg_html([
            {"title": "Rescued", "href": "https://rescued.com", "snippet": "via curl"},
        ])
        called = {"curl": 0}

        async def fake_httpx(data):
            return 202, "<html><body>empty block page</body></html>"

        async def fake_curl(data):
            called["curl"] += 1
            return html

        with patch.object(searcher, "_request_httpx", side_effect=fake_httpx), \
             patch.object(searcher, "_request_curl", side_effect=fake_curl):
            results = asyncio.run(searcher.search("q", DummyCtx()))

        self.assertEqual(called["curl"], 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Rescued")

    def test_auto_falls_back_to_curl_on_403(self):
        searcher = DuckDuckGoSearcher(backend="auto")
        html = _make_ddg_html([
            {"title": "Rescued", "href": "https://rescued.com", "snippet": "via curl"},
        ])
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        err = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=mock_resp)
        called = {"curl": 0}

        async def fake_httpx(data):
            raise err

        async def fake_curl(data):
            called["curl"] += 1
            return html

        with patch.object(searcher, "_request_httpx", side_effect=fake_httpx), \
             patch.object(searcher, "_request_curl", side_effect=fake_curl):
            results = asyncio.run(searcher.search("q", DummyCtx()))

        self.assertEqual(called["curl"], 1)
        self.assertEqual(len(results), 1)

    def test_auto_does_not_fall_back_on_normal_results(self):
        searcher = DuckDuckGoSearcher(backend="auto")
        html = _make_ddg_html([
            {"title": "Normal", "href": "https://normal.com", "snippet": "ok"},
        ])
        called = {"curl": 0}

        async def fake_httpx(data):
            return 200, html

        async def fake_curl(data):
            called["curl"] += 1
            return "<html></html>"

        with patch.object(searcher, "_request_httpx", side_effect=fake_httpx), \
             patch.object(searcher, "_request_curl", side_effect=fake_curl):
            results = asyncio.run(searcher.search("q", DummyCtx()))

        self.assertEqual(called["curl"], 0)
        self.assertEqual(len(results), 1)

    def test_auto_falls_back_to_curl_on_connect_error(self):
        """A rejected TLS handshake (httpx.ConnectError) should retry with curl."""
        searcher = DuckDuckGoSearcher(backend="auto")
        html = _make_ddg_html([
            {"title": "Rescued", "href": "https://rescued.com", "snippet": "via curl"},
        ])
        called = {"curl": 0}

        async def fake_httpx(data):
            raise httpx.ConnectError("TLS handshake rejected")

        async def fake_curl(data):
            called["curl"] += 1
            return html

        with patch.object(searcher, "_request_httpx", side_effect=fake_httpx), \
             patch.object(searcher, "_request_curl", side_effect=fake_curl):
            results = asyncio.run(searcher.search("q", DummyCtx()))

        self.assertEqual(called["curl"], 1)
        self.assertEqual(len(results), 1)

    def test_empty_results_message_omits_hint_when_curl_installed(self):
        """When curl_cffi is available the 'install [browser]' hint is dropped."""
        searcher = DuckDuckGoSearcher()
        with patch("duckduckgo_mcp_server.server._curl_cffi_available", return_value=True):
            message = searcher.format_results_for_llm([])
        self.assertIn("No results were found", message)
        self.assertNotIn("pip install", message)

    def test_empty_results_message_includes_hint_when_curl_missing(self):
        searcher = DuckDuckGoSearcher()
        with patch("duckduckgo_mcp_server.server._curl_cffi_available", return_value=False):
            message = searcher.format_results_for_llm([])
        self.assertIn("pip install 'duckduckgo-mcp-server[browser]'", message)

    def test_httpx_backend_does_not_fall_back_on_202(self):
        """Explicit httpx backend keeps legacy behavior: 202 → 0 results, no curl."""
        searcher = DuckDuckGoSearcher(backend="httpx")
        called = {"curl": 0}

        async def fake_httpx(data):
            return 202, "<html><body>empty block page</body></html>"

        async def fake_curl(data):
            called["curl"] += 1
            return "should not be called"

        with patch.object(searcher, "_request_httpx", side_effect=fake_httpx), \
             patch.object(searcher, "_request_curl", side_effect=fake_curl):
            results = asyncio.run(searcher.search("q", DummyCtx()))

        self.assertEqual(called["curl"], 0)
        self.assertEqual(results, [])

    def test_curl_backend_missing_dependency_returns_empty(self):
        """curl backend with curl_cffi absent → empty results (hint logged), no crash."""
        searcher = DuckDuckGoSearcher(backend="curl")
        with patch.dict(sys.modules, {"curl_cffi": None, "curl_cffi.requests": None}):
            results = asyncio.run(searcher.search("q", DummyCtx()))
        self.assertEqual(results, [])


def _serve_html(html_content):
    """Spin up a throwaway local HTTP server serving html_content. Returns (url, stop_fn)."""

    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), SimpleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{server.server_address[1]}"

    def stop():
        server.shutdown()
        thread.join()

    return url, stop


# Backends to exercise in the parameterized fetcher tests. curl is only included
# when curl_cffi is actually installed (the optional [browser] extra).
_FETCH_BACKENDS_FOR_TESTING = ["httpx"] + (["curl"] if HAS_CURL_CFFI else [])


class TestWebContentFetcher(unittest.TestCase):
    def test_fetch_and_parse_extracts_clean_text(self):
        html_content = """
        <html>
            <head>
                <title>Example</title>
                <script>console.log('ignored');</script>
                <style>body { background: #fff; }</style>
            </head>
            <body>
                <nav>Navigation</nav>
                <header>Header</header>
                <h1>Sample Heading</h1>
                <p>Some meaningful paragraph.</p>
                <footer>Footer</footer>
            </body>
        </html>
        """

        url, stop = _serve_html(html_content)
        try:
            for backend in _FETCH_BACKENDS_FOR_TESTING:
                with self.subTest(backend=backend):
                    # Local server is on 127.0.0.1, so opt into private URLs.
                    fetcher = WebContentFetcher(backend=backend, allow_private_urls=True)
                    text = asyncio.run(fetcher.fetch_and_parse(url, DummyCtx()))
                    self.assertIn("Sample Heading", text)
                    self.assertIn("Some meaningful paragraph.", text)
                    self.assertNotIn("Navigation", text)
                    self.assertNotIn("console.log", text)
        finally:
            stop()

    def test_fetch_and_parse_pagination(self):
        html_content = "<html><body><p>" + "A" * 100 + "</p></body></html>"
        url, stop = _serve_html(html_content)
        try:
            for backend in _FETCH_BACKENDS_FOR_TESTING:
                with self.subTest(backend=backend):
                    fetcher = WebContentFetcher(backend=backend, allow_private_urls=True)
                    # Fetch first 50 chars
                    text = asyncio.run(
                        fetcher.fetch_and_parse(url, DummyCtx(), start_index=0, max_length=50)
                    )
                    self.assertIn("start_index=50 to see more", text)
                    self.assertIn("of 100 total", text)
                    # Fetch from offset 50
                    text = asyncio.run(
                        fetcher.fetch_and_parse(url, DummyCtx(), start_index=50, max_length=50)
                    )
                    self.assertNotIn("to see more", text)
                    self.assertIn("of 100 total", text)
        finally:
            stop()


class TestWebContentFetcherCache(unittest.TestCase):
    def test_pagination_reuses_one_download(self):
        html = "<html><body><p>" + "A" * 100 + "</p></body></html>"
        fetcher = WebContentFetcher(backend="httpx", allow_private_urls=True)
        fetch_count = {"n": 0}

        async def fake_httpx(url):
            fetch_count["n"] += 1
            return html

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx):
            first = asyncio.run(
                fetcher.fetch_and_parse("https://example.com/page", DummyCtx(), start_index=0, max_length=50)
            )
            second = asyncio.run(
                fetcher.fetch_and_parse("https://example.com/page", DummyCtx(), start_index=50, max_length=50)
            )

        self.assertEqual(fetch_count["n"], 1)
        self.assertIn("cache=miss", first)
        self.assertIn("cache=hit", second)
        self.assertIn("start_index=50 to see more", first)
        self.assertNotIn("to see more", second)

    def test_disabled_cache_refetches(self):
        html = "<html><body><p>Hello</p></body></html>"
        fetcher = WebContentFetcher(
            backend="httpx", allow_private_urls=True, cache_ttl=0
        )
        fetch_count = {"n": 0}

        async def fake_httpx(url):
            fetch_count["n"] += 1
            return html

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx):
            asyncio.run(fetcher.fetch_and_parse("https://example.com/page", DummyCtx()))
            asyncio.run(fetcher.fetch_and_parse("https://example.com/page", DummyCtx()))

        self.assertEqual(fetch_count["n"], 2)

    def test_errors_are_not_cached(self):
        fetcher = WebContentFetcher(backend="httpx", allow_private_urls=True)
        calls = {"n": 0}

        async def fake_httpx(url):
            calls["n"] += 1
            raise httpx.TimeoutException("timed out")

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx):
            first = asyncio.run(fetcher.fetch_and_parse("https://example.com/page", DummyCtx()))
            second = asyncio.run(fetcher.fetch_and_parse("https://example.com/page", DummyCtx()))

        self.assertEqual(calls["n"], 2)
        self.assertTrue(first.startswith("Error"))
        self.assertTrue(second.startswith("Error"))
        self.assertEqual(len(fetcher.cache), 0)

    def test_cache_hit_skips_rate_limiter(self):
        html = "<html><body><p>Cached</p></body></html>"
        fetcher = WebContentFetcher(backend="httpx", allow_private_urls=True)
        limiter_calls = {"n": 0}
        original_acquire = fetcher.rate_limiter.acquire

        async def counting_acquire():
            limiter_calls["n"] += 1
            await original_acquire()

        async def fake_httpx(url):
            return html

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx), \
             patch.object(fetcher.rate_limiter, "acquire", side_effect=counting_acquire):
            asyncio.run(fetcher.fetch_and_parse("https://example.com/page", DummyCtx()))
            asyncio.run(fetcher.fetch_and_parse("https://example.com/page", DummyCtx()))

        self.assertEqual(limiter_calls["n"], 1)

    def test_fragment_does_not_split_cache_entries(self):
        html = "<html><body><p>Same page</p></body></html>"
        fetcher = WebContentFetcher(backend="httpx", allow_private_urls=True)
        fetch_count = {"n": 0}

        async def fake_httpx(url):
            fetch_count["n"] += 1
            return html

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx):
            asyncio.run(fetcher.fetch_and_parse("https://example.com/a#one", DummyCtx()))
            asyncio.run(fetcher.fetch_and_parse("https://example.com/a#two", DummyCtx()))

        self.assertEqual(fetch_count["n"], 1)


_ARTICLE_HTML = """
<html>
  <body>
    <nav>Site Nav</nav>
    <aside>Related junk</aside>
    <article>
      <h1>Primary Title</h1>
      <p>The real article paragraph with <a href="https://ex.com/more">a link</a>.</p>
      <ul>
        <li>First item</li>
        <li>Second item</li>
      </ul>
      <pre>code_sample()</pre>
    </article>
    <footer>Copyright</footer>
  </body>
</html>
"""


class TestParseModes(unittest.TestCase):
    def test_supported_modes(self):
        self.assertEqual(SUPPORTED_PARSE_MODES, ("text", "main", "markdown"))

    def test_text_mode_includes_non_chrome_siblings(self):
        # aside is now treated as chrome and stripped; leftover non-article
        # text still appears in text mode when it is not chrome.
        html = "<html><body><article><p>Inside</p></article><section>Outside section</section></body></html>"
        text = _html_to_text(html, "text")
        self.assertIn("Inside", text)
        self.assertIn("Outside section", text)

    def test_main_mode_drops_sidebar_and_keeps_article(self):
        text = _html_to_text(_ARTICLE_HTML, "main")
        self.assertIn("Primary Title", text)
        self.assertIn("real article paragraph", text)
        self.assertNotIn("Site Nav", text)
        self.assertNotIn("Related junk", text)
        self.assertNotIn("Copyright", text)

    def test_markdown_mode_preserves_structure(self):
        md = _html_to_text(_ARTICLE_HTML, "markdown")
        self.assertIn("# Primary Title", md)
        self.assertIn("[a link](https://ex.com/more)", md)
        self.assertIn("- First item", md)
        self.assertIn("- Second item", md)
        self.assertIn("```", md)
        self.assertIn("code_sample()", md)
        self.assertNotIn("Site Nav", md)
        self.assertNotIn("Related junk", md)

    def test_markdown_href_allows_only_http_https(self):
        self.assertEqual(_safe_markdown_href("https://ex.com/a"), "https://ex.com/a")
        self.assertEqual(_safe_markdown_href("http://ex.com/a"), "http://ex.com/a")
        self.assertIsNone(_safe_markdown_href("javascript:alert(1)"))
        self.assertIsNone(_safe_markdown_href("data:text/html,x"))
        self.assertIsNone(_safe_markdown_href("/relative"))
        self.assertIsNone(_safe_markdown_href("https://ex.com/a\n) extra"))

    def test_markdown_mode_drops_javascript_links(self):
        html = (
            "<html><body><article><p>See "
            '<a href="javascript:alert(1)">bad</a> and '
            '<a href="https://ok.example/x">good</a>.'
            "</p></article></body></html>"
        )
        md = _html_to_text(html, "markdown")
        self.assertNotIn("javascript:", md)
        self.assertIn("[good](https://ok.example/x)", md)
        self.assertIn("bad", md)

    def test_main_mode_falls_back_to_body_without_container(self):
        html = (
            "<html><head><title>T</title></head><body><nav>Menu</nav>"
            "<div><p>Left column</p></div><div><p>Right column</p></div>"
            "</body></html>"
        )
        text = _html_to_text(html, "main")
        self.assertIn("Left column", text)
        self.assertIn("Right column", text)
        self.assertNotIn("Menu", text)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            _html_to_text("<p>x</p>", "bogus")

    def test_init_rejects_unknown_parse_mode(self):
        with self.assertRaises(ValueError):
            WebContentFetcher(parse_mode="bogus")

    def test_per_call_unknown_parse_mode_returns_error(self):
        fetcher = WebContentFetcher()
        result = asyncio.run(
            fetcher.fetch_and_parse("https://example.com", DummyCtx(), parse_mode="bogus")
        )
        self.assertIn("Unknown parse_mode", result)

    def test_parse_modes_use_separate_cache_entries(self):
        fetcher = WebContentFetcher(backend="httpx", allow_private_urls=True)
        fetch_count = {"n": 0}

        async def fake_httpx(url):
            fetch_count["n"] += 1
            return _ARTICLE_HTML

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx):
            text = asyncio.run(
                fetcher.fetch_and_parse("https://example.com/a", DummyCtx(), parse_mode="text")
            )
            main = asyncio.run(
                fetcher.fetch_and_parse("https://example.com/a", DummyCtx(), parse_mode="main")
            )
            again = asyncio.run(
                fetcher.fetch_and_parse("https://example.com/a", DummyCtx(), parse_mode="text")
            )

        self.assertEqual(fetch_count["n"], 2)
        # The historical trailer is unchanged in the default mode.
        self.assertNotIn("parse=", text)
        self.assertIn("parse=main", main)
        self.assertIn("cache=hit", again)

    def test_main_parses_parse_mode_flag(self):
        with patch.object(sys, "argv", ["duckduckgo-mcp-server", "--parse-mode", "markdown"]), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertEqual(duckduckgo_mcp_server.server.fetcher.default_parse_mode, "markdown")


def _patch_backend_client(backend, *, get_return_value=None, get_side_effect=None):
    """Return a context manager that patches the HTTP client for the given backend.

    - "httpx": patches `httpx.AsyncClient`.
    - "curl":  patches `curl_cffi.requests.AsyncSession`.
    Both are patched with an AsyncMock whose .get() uses the provided return/side-effect.
    """
    mock_client = AsyncMock()
    if get_side_effect is not None:
        mock_client.get = AsyncMock(side_effect=get_side_effect)
    else:
        mock_client.get = AsyncMock(return_value=get_return_value)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    if backend == "httpx":
        return patch("httpx.AsyncClient", return_value=mock_client)
    elif backend == "curl":
        return patch("curl_cffi.requests.AsyncSession", return_value=mock_client)
    raise ValueError(f"no patcher for backend {backend!r}")


class TestWebContentFetcherErrors(unittest.TestCase):
    def test_fetch_returns_error_on_timeout(self):
        for backend in _FETCH_BACKENDS_FOR_TESTING:
            with self.subTest(backend=backend):
                # These mock the HTTP client; skip the SSRF guard (no real DNS).
                fetcher = WebContentFetcher(backend=backend, allow_private_urls=True)
                # Use an exception whose type-name triggers the server's curl-path
                # error handling without needing curl_cffi's exception hierarchy.
                exc = httpx.TimeoutException("timed out") if backend == "httpx" else TimeoutError("timed out")
                with _patch_backend_client(backend, get_side_effect=exc):
                    result = asyncio.run(
                        fetcher.fetch_and_parse("https://example.com", DummyCtx())
                    )
                self.assertTrue(result.startswith("Error"), f"got: {result!r}")
                self.assertIn("timed out", result.lower())

    def test_fetch_returns_error_on_http_error(self):
        for backend in _FETCH_BACKENDS_FOR_TESTING:
            with self.subTest(backend=backend):
                # These mock the HTTP client; skip the SSRF guard (no real DNS).
                fetcher = WebContentFetcher(backend=backend, allow_private_urls=True)
                mock_resp = MagicMock()
                mock_resp.status_code = 500
                mock_resp.request = MagicMock()
                if backend == "httpx":
                    err = httpx.HTTPStatusError("server error", request=mock_resp.request, response=mock_resp)
                else:
                    err = RuntimeError("curl http 500")
                mock_resp.raise_for_status = MagicMock(side_effect=err)
                with _patch_backend_client(backend, get_return_value=mock_resp):
                    result = asyncio.run(
                        fetcher.fetch_and_parse("https://example.com", DummyCtx())
                    )
                self.assertTrue(result.startswith("Error"), f"got: {result!r}")

    def test_fetch_handles_malformed_html(self):
        for backend in _FETCH_BACKENDS_FOR_TESTING:
            with self.subTest(backend=backend):
                # These mock the HTTP client; skip the SSRF guard (no real DNS).
                fetcher = WebContentFetcher(backend=backend, allow_private_urls=True)
                mock_resp = MagicMock()
                mock_resp.text = "<<<not valid>>>"
                mock_resp.status_code = 200
                mock_resp.raise_for_status = MagicMock()
                with _patch_backend_client(backend, get_return_value=mock_resp):
                    result = asyncio.run(
                        fetcher.fetch_and_parse("https://example.com", DummyCtx())
                    )
                # Should not crash - returns some text (possibly empty or with metadata)
                self.assertIsInstance(result, str)


class TestWebContentFetcherBackend(unittest.TestCase):
    def test_init_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            WebContentFetcher(backend="bogus")

    def test_default_backend_is_httpx(self):
        self.assertEqual(WebContentFetcher().default_backend, "httpx")

    def test_supported_backends_tuple(self):
        self.assertEqual(SUPPORTED_FETCH_BACKENDS, ("httpx", "curl", "auto"))

    def test_per_call_backend_overrides_default(self):
        """default=httpx, pass backend='curl' per-call → curl path is exercised."""
        fetcher = WebContentFetcher(backend="httpx")
        ctx = DummyCtx()
        called = {"httpx": False, "curl": False}

        async def fake_httpx(url):
            called["httpx"] = True
            return "<html><body><p>from httpx</p></body></html>"

        async def fake_curl(url):
            called["curl"] = True
            return "<html><body><p>from curl</p></body></html>"

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx), \
             patch.object(fetcher, "_fetch_curl", side_effect=fake_curl):
            text = asyncio.run(
                fetcher.fetch_and_parse("https://example.com", ctx, backend="curl")
            )

        self.assertFalse(called["httpx"])
        self.assertTrue(called["curl"])
        self.assertIn("from curl", text)

    def test_per_call_unknown_backend_returns_error(self):
        fetcher = WebContentFetcher()
        result = asyncio.run(
            fetcher.fetch_and_parse("https://example.com", DummyCtx(), backend="bogus")
        )
        self.assertIn("Unknown fetch backend", result)

    def test_curl_backend_missing_dependency_error(self):
        """If curl_cffi isn't importable, curl backend returns a helpful install hint."""
        fetcher = WebContentFetcher(backend="curl")
        # Make the lazy `from curl_cffi.requests import AsyncSession` raise ImportError.
        with patch.dict(sys.modules, {"curl_cffi": None, "curl_cffi.requests": None}):
            result = asyncio.run(
                fetcher.fetch_and_parse("https://example.com", DummyCtx())
            )
        self.assertIn("Error", result)
        self.assertIn("pip install", result)
        self.assertIn("browser", result)


class TestWebContentFetcherAutoFallback(unittest.TestCase):
    def test_auto_uses_httpx_when_successful(self):
        fetcher = WebContentFetcher(backend="auto")
        called = {"httpx": 0, "curl": 0}

        async def fake_httpx(url):
            called["httpx"] += 1
            return "<html><body><p>ok from httpx</p></body></html>"

        async def fake_curl(url):
            called["curl"] += 1
            return "<html><body><p>from curl</p></body></html>"

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx), \
             patch.object(fetcher, "_fetch_curl", side_effect=fake_curl):
            text = asyncio.run(fetcher.fetch_and_parse("https://example.com", DummyCtx()))

        self.assertEqual(called["httpx"], 1)
        self.assertEqual(called["curl"], 0)
        self.assertIn("ok from httpx", text)

    def test_auto_falls_back_on_403(self):
        fetcher = WebContentFetcher(backend="auto")
        called = {"curl": 0}

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        err = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=mock_resp)

        async def fake_httpx(url):
            raise err

        async def fake_curl(url):
            called["curl"] += 1
            return "<html><body><p>rescued by curl</p></body></html>"

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx), \
             patch.object(fetcher, "_fetch_curl", side_effect=fake_curl):
            text = asyncio.run(fetcher.fetch_and_parse("https://example.com", DummyCtx()))

        self.assertEqual(called["curl"], 1)
        self.assertIn("rescued by curl", text)

    def test_auto_falls_back_on_cloudflare_challenge(self):
        fetcher = WebContentFetcher(backend="auto")
        called = {"curl": 0}

        async def fake_httpx(url):
            return (
                "<html><head><title>Just a moment...</title></head>"
                "<body>Enable JavaScript and cookies to continue</body></html>"
            )

        async def fake_curl(url):
            called["curl"] += 1
            return "<html><body><p>real content</p></body></html>"

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx), \
             patch.object(fetcher, "_fetch_curl", side_effect=fake_curl):
            text = asyncio.run(fetcher.fetch_and_parse("https://example.com", DummyCtx()))

        self.assertEqual(called["curl"], 1)
        self.assertIn("real content", text)

    def test_auto_reraises_non_403_http_error(self):
        """A 500 under auto should NOT trigger curl fallback — only 403/CF signals do."""
        fetcher = WebContentFetcher(backend="auto")
        called = {"curl": 0}

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        err = httpx.HTTPStatusError("server error", request=MagicMock(), response=mock_resp)

        async def fake_httpx(url):
            raise err

        async def fake_curl(url):
            called["curl"] += 1
            return "<html></html>"

        with patch.object(fetcher, "_fetch_httpx", side_effect=fake_httpx), \
             patch.object(fetcher, "_fetch_curl", side_effect=fake_curl):
            result = asyncio.run(fetcher.fetch_and_parse("https://example.com", DummyCtx()))

        self.assertEqual(called["curl"], 0)
        self.assertTrue(result.startswith("Error"))


class TestSSRFGuard(unittest.TestCase):
    def _assert_blocked(self, url):
        with self.assertRaises(BlockedURLError):
            asyncio.run(_validate_public_url(url))

    def test_rejects_loopback_ip(self):
        self._assert_blocked("http://127.0.0.1/")
        self._assert_blocked("http://127.0.0.1:8080/latest/meta-data/")

    def test_rejects_localhost_hostname(self):
        self._assert_blocked("http://localhost/")
        self._assert_blocked("https://sub.localhost/")

    def test_rejects_cloud_metadata_ip(self):
        self._assert_blocked("http://169.254.169.254/latest/meta-data/")

    def test_rejects_private_ips(self):
        for host in ("10.0.0.1", "192.168.1.1", "172.16.5.4"):
            with self.subTest(host=host):
                self._assert_blocked(f"http://{host}/")

    def test_rejects_unspecified_and_ipv6_loopback(self):
        self._assert_blocked("http://0.0.0.0/")
        self._assert_blocked("http://[::1]/")

    def test_rejects_ipv4_mapped_ipv6_loopback(self):
        # Either resolves to an IPv4 loopback or fails to resolve — both are blocked.
        self._assert_blocked("http://[::ffff:127.0.0.1]/")

    def test_rejects_cgnat_shared_address_space(self):
        # RFC 6598 100.64.0.0/10 is not is_private/is_reserved but is non-global;
        # it's used by CGNAT and overlay networks like Tailscale.
        self._assert_blocked("http://100.64.0.1/")
        self._assert_blocked("http://100.127.255.254/")

    def test_rejects_invalid_port(self):
        # An out-of-range port makes urllib's .port raise ValueError; the guard
        # should surface a clean BlockedURLError, not a generic failure.
        self._assert_blocked("http://example.com:99999/")

    def test_rejects_non_http_scheme(self):
        self._assert_blocked("file:///etc/passwd")
        self._assert_blocked("ftp://example.com/x")
        self._assert_blocked("gopher://127.0.0.1/")

    def test_allows_public_ip_literals(self):
        # Public IPs must pass. IP literals avoid a real DNS lookup.
        for url in ("http://1.1.1.1/", "https://8.8.8.8/"):
            with self.subTest(url=url):
                asyncio.run(_validate_public_url(url))  # must not raise

    def test_fetch_content_blocks_localhost_by_default(self):
        fetcher = WebContentFetcher()
        result = asyncio.run(fetcher.fetch_and_parse("http://127.0.0.1:9/", DummyCtx()))
        self.assertIn("Refusing to fetch", result)
        self.assertIn("DDG_ALLOW_PRIVATE_URLS", result)

    def test_fetch_content_blocks_metadata_by_default(self):
        fetcher = WebContentFetcher()
        result = asyncio.run(
            fetcher.fetch_and_parse("http://169.254.169.254/latest/meta-data/", DummyCtx())
        )
        self.assertIn("Refusing to fetch", result)

    def test_fetch_content_allows_private_when_opted_in(self):
        html = "<html><body><h1>Internal OK</h1></body></html>"
        url, stop = _serve_html(html)
        try:
            fetcher = WebContentFetcher(allow_private_urls=True)
            result = asyncio.run(fetcher.fetch_and_parse(url, DummyCtx()))
            self.assertIn("Internal OK", result)
        finally:
            stop()

    def test_redirect_to_private_is_blocked(self):
        """A public entry URL that 302-redirects to a private host is blocked mid-hop."""
        fetcher = WebContentFetcher()  # default-deny
        redirect_resp = MagicMock()
        redirect_resp.status_code = 302
        redirect_resp.headers = {"location": "http://127.0.0.1/secret"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=redirect_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(fetcher.fetch_and_parse("http://1.1.1.1/", DummyCtx()))

        self.assertIn("Refusing to fetch", result)
        self.assertIn("127.0.0.1", result)


def _setup_mock_mcp_for_http(mock_mcp):
    sse_app = MagicMock()
    sse_app.router.lifespan_context = MagicMock(name="sse_lifespan")
    http_app = MagicMock()
    http_app.router.lifespan_context = MagicMock(name="http_lifespan")

    mock_mcp.sse_app.return_value = sse_app
    mock_mcp.streamable_http_app.return_value = http_app
    sse_app.routes = []
    http_app.routes = []
    return sse_app, http_app


class TestMainCliArgs(unittest.TestCase):
    def test_main_parses_fetch_backend_flag(self):
        with patch.object(sys, "argv", ["duckduckgo-mcp-server", "--fetch-backend", "auto"]), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertEqual(duckduckgo_mcp_server.server.fetcher.default_backend, "auto")

    def test_main_defaults_to_httpx(self):
        with patch.object(sys, "argv", ["duckduckgo-mcp-server"]), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertEqual(duckduckgo_mcp_server.server.fetcher.default_backend, "httpx")

    def test_main_parses_cache_flags(self):
        with patch.object(
            sys, "argv", ["duckduckgo-mcp-server", "--cache-ttl", "0", "--cache-max-entries", "3"]
        ), patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertFalse(duckduckgo_mcp_server.server.fetcher.cache.enabled)
        self.assertEqual(duckduckgo_mcp_server.server.fetcher.cache.max_entries, 3)

    def test_main_rejects_negative_cache_ttl(self):
        with patch.object(sys, "argv", ["duckduckgo-mcp-server", "--cache-ttl", "-1"]), \
             patch("duckduckgo_mcp_server.server.mcp"):
            with self.assertRaises(SystemExit):
                duckduckgo_mcp_server.server.main()

    def test_main_parses_search_backend_flag(self):
        with patch.object(sys, "argv", ["duckduckgo-mcp-server", "--search-backend", "curl"]), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertEqual(duckduckgo_mcp_server.server.searcher.backend, "curl")

    def test_main_stdio_rejects_mixed_with_http(self):
        for bad_transports in [
            ["stdio", "sse"],
            ["stdio", "streamable-http"],
            ["stdio", "sse", "streamable-http"],
        ]:
            with self.subTest(transports=bad_transports):
                argv = ["duckduckgo-mcp-server", "--transport"] + bad_transports
                with patch.object(sys, "argv", argv), \
                     patch("duckduckgo_mcp_server.server.mcp"):
                    with self.assertRaises(SystemExit):
                        duckduckgo_mcp_server.server.main()

    def test_main_applies_host_and_port_to_apps(self):
        argv = [
            "duckduckgo-mcp-server",
            "--transport", "streamable-http",
            "--host", "0.0.0.0",
            "--port", "7070",
        ]
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp, \
             patch("uvicorn.run") as mock_uvicorn_run:
            _setup_mock_mcp_for_http(mock_mcp)
            duckduckgo_mcp_server.server.main()
            # The bind host reaches both app factories (it decides whether the
            # SDK auto-enables DNS-rebinding protection); the port goes to uvicorn.
            self.assertEqual(mock_mcp.sse_app.call_args.kwargs["host"], "0.0.0.0")
            self.assertEqual(mock_mcp.streamable_http_app.call_args.kwargs["host"], "0.0.0.0")
            mock_uvicorn_run.assert_called_once()
            call_kwargs = mock_uvicorn_run.call_args.kwargs
            self.assertEqual(call_kwargs["host"], "0.0.0.0")
            self.assertEqual(call_kwargs["port"], 7070)

    def test_main_route_dedup_prevents_duplicates(self):
        argv = ["duckduckgo-mcp-server", "--transport", "sse", "streamable-http"]
        async def handler(request):
            pass
        shared = StarletteRoute("/common", handler, methods=["GET"])
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp, \
             patch("uvicorn.run") as mock_uvicorn_run:
            sse_app, http_app = _setup_mock_mcp_for_http(mock_mcp)
            sse_app.routes = [shared]
            http_app.routes = [shared]
            duckduckgo_mcp_server.server.main()
            app = mock_uvicorn_run.call_args[0][0]
            matching = [
                r for r in app.routes
                if isinstance(r, StarletteRoute) and r.path == "/common" and "GET" in r.methods
            ]
            self.assertEqual(len(matching), 1, "Same (path, method) should be deduplicated")

    def test_main_route_dedup_allows_different_methods(self):
        argv = ["duckduckgo-mcp-server", "--transport", "sse", "streamable-http"]
        async def handler(request):
            pass
        get_route = StarletteRoute("/common", handler, methods=["GET"])
        post_route = StarletteRoute("/common", handler, methods=["POST"])
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp, \
             patch("uvicorn.run") as mock_uvicorn_run:
            sse_app, http_app = _setup_mock_mcp_for_http(mock_mcp)
            sse_app.routes = [get_route]
            http_app.routes = [post_route]
            duckduckgo_mcp_server.server.main()
            app = mock_uvicorn_run.call_args[0][0]
            matching = [
                r for r in app.routes
                if isinstance(r, StarletteRoute) and r.path == "/common"
            ]
            self.assertEqual(len(matching), 2, "Same path with different methods should both be added")
            self.assertTrue(any("GET" in r.methods for r in matching))
            self.assertTrue(any("POST" in r.methods for r in matching))

    def test_main_lifespan_selection(self):
        for transports, expected_lifespan_name in [
            (["sse"], "sse_lifespan"),
            (["streamable-http"], "http_lifespan"),
            (["sse", "streamable-http"], "combined"),
        ]:
            with self.subTest(transports=transports):
                argv = ["duckduckgo-mcp-server", "--transport"] + transports
                with patch.object(sys, "argv", argv), \
                     patch("duckduckgo_mcp_server.server.mcp") as mock_mcp, \
                     patch("uvicorn.run") as mock_uvicorn_run:
                    sse_app, http_app = _setup_mock_mcp_for_http(mock_mcp)
                    duckduckgo_mcp_server.server.main()
                    app = mock_uvicorn_run.call_args[0][0]
                    lifespan = app.router.lifespan_context
                    if expected_lifespan_name == "combined":
                        self.assertIsNot(lifespan, sse_app.router.lifespan_context)
                        self.assertIsNot(lifespan, http_app.router.lifespan_context)
                    else:
                        self.assertEqual(
                            lifespan._mock_name,
                            expected_lifespan_name,
                            f"Wrong lifespan for {transports}",
                        )

    def test_main_stdio_rejects_host_port(self):
        for bad_arg in (
            ["--host", "0.0.0.0"],
            ["--port", "7070"],
            ["--host", "0.0.0.0", "--port", "7070"],
        ):
            with self.subTest(bad_arg=bad_arg):
                argv = ["duckduckgo-mcp-server"] + bad_arg
                with patch.object(sys, "argv", argv), \
                     patch("duckduckgo_mcp_server.server.mcp"):
                    with self.assertRaises(SystemExit):
                        duckduckgo_mcp_server.server.main()

    def test_main_http_uses_default_host_port(self):
        argv = ["duckduckgo-mcp-server", "--transport", "streamable-http"]
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp, \
             patch("uvicorn.run") as mock_uvicorn_run:
            _setup_mock_mcp_for_http(mock_mcp)
            duckduckgo_mcp_server.server.main()
            self.assertEqual(mock_mcp.streamable_http_app.call_args.kwargs["host"], "127.0.0.1")
            self.assertEqual(mock_mcp.streamable_http_app.call_args.kwargs["streamable_http_path"], "/mcp")
            self.assertEqual(mock_mcp.sse_app.call_args.kwargs["sse_path"], "/sse")
            call_kwargs = mock_uvicorn_run.call_args.kwargs
            self.assertEqual(call_kwargs["host"], "127.0.0.1")
            self.assertEqual(call_kwargs["port"], 8000)


class TestTransportSecurity(unittest.TestCase):
    def test_build_returns_none_when_unset(self):
        # Nothing configured → keep the SDK's secure default (None).
        self.assertIsNone(_build_transport_security([], [], False))

    def test_build_allowlist_keeps_protection_on(self):
        ts = _build_transport_security(["ex.com:*"], ["http://ex.com:*"], False)
        self.assertTrue(ts.enable_dns_rebinding_protection)
        self.assertEqual(ts.allowed_hosts, ["ex.com:*"])
        self.assertEqual(ts.allowed_origins, ["http://ex.com:*"])

    def test_build_disable_turns_protection_off(self):
        ts = _build_transport_security([], [], True)
        self.assertIsNotNone(ts)
        self.assertFalse(ts.enable_dns_rebinding_protection)

    def test_main_applies_allowed_hosts(self):
        argv = [
            "duckduckgo-mcp-server", "--transport", "streamable-http",
            "--allowed-hosts", "ddg.example.com", "ddg.example.com:*",
        ]
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp, \
             patch("uvicorn.run"):
            _setup_mock_mcp_for_http(mock_mcp)
            duckduckgo_mcp_server.server.main()
            ts = mock_mcp.streamable_http_app.call_args.kwargs["transport_security"]
            self.assertIs(mock_mcp.sse_app.call_args.kwargs["transport_security"], ts)
            self.assertTrue(ts.enable_dns_rebinding_protection)
            self.assertEqual(ts.allowed_hosts, ["ddg.example.com", "ddg.example.com:*"])

    def test_main_disable_dns_rebinding_protection(self):
        argv = [
            "duckduckgo-mcp-server", "--transport", "sse",
            "--disable-dns-rebinding-protection",
        ]
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp, \
             patch("uvicorn.run"):
            _setup_mock_mcp_for_http(mock_mcp)
            duckduckgo_mcp_server.server.main()
            ts = mock_mcp.sse_app.call_args.kwargs["transport_security"]
            self.assertFalse(ts.enable_dns_rebinding_protection)


class TestSSLVerifyConfig(unittest.TestCase):
    def test_resolve_ssl_verify(self):
        # Default: verification on with the client's own trust store.
        self.assertIs(_resolve_ssl_verify(""), True)
        # A CA bundle path is passed through as the verify value.
        self.assertEqual(_resolve_ssl_verify("/etc/proxy-ca.pem"), "/etc/proxy-ca.pem")
        # Disabling verification wins over a CA bundle.
        self.assertIs(_resolve_ssl_verify("/etc/proxy-ca.pem", verify_enabled=False), False)
        self.assertIs(_resolve_ssl_verify("", verify_enabled=False), False)

    def test_defaults_to_verified(self):
        self.assertIs(DuckDuckGoSearcher().ssl_verify, True)
        self.assertIs(WebContentFetcher().ssl_verify, True)

    def test_searcher_passes_verify_to_httpx_client(self):
        searcher = DuckDuckGoSearcher(backend="httpx", ssl_verify="/etc/proxy-ca.pem")
        mock_resp = _mock_post_response("<html><body></body></html>")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            asyncio.run(searcher.search("test", DummyCtx()))

        self.assertEqual(mock_cls.call_args.kwargs.get("verify"), "/etc/proxy-ca.pem")

    def test_fetcher_passes_verify_to_httpx_client(self):
        fetcher = WebContentFetcher(allow_private_urls=True, ssl_verify=False)
        mock_resp = MagicMock()
        mock_resp.text = "<html><body><p>ok</p></body></html>"
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            asyncio.run(fetcher.fetch_and_parse("https://example.com", DummyCtx()))

        self.assertIs(mock_cls.call_args.kwargs.get("verify"), False)

    def test_main_parses_ca_certs_flag(self):
        with tempfile.NamedTemporaryFile(suffix=".pem") as ca_file:
            argv = ["duckduckgo-mcp-server", "--ca-certs", ca_file.name]
            with patch.object(sys, "argv", argv), \
                 patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
                duckduckgo_mcp_server.server.main()
                mock_mcp.run.assert_called_once()
            self.assertEqual(duckduckgo_mcp_server.server.fetcher.ssl_verify, ca_file.name)
            self.assertEqual(duckduckgo_mcp_server.server.searcher.ssl_verify, ca_file.name)

    def test_main_parses_no_ssl_verify_flag(self):
        argv = ["duckduckgo-mcp-server", "--no-ssl-verify"]
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp") as mock_mcp:
            duckduckgo_mcp_server.server.main()
            mock_mcp.run.assert_called_once()
        self.assertIs(duckduckgo_mcp_server.server.fetcher.ssl_verify, False)
        self.assertIs(duckduckgo_mcp_server.server.searcher.ssl_verify, False)

    def test_main_rejects_missing_ca_certs_path(self):
        argv = ["duckduckgo-mcp-server", "--ca-certs", "/nonexistent/ca-bundle.pem"]
        with patch.object(sys, "argv", argv), \
             patch("duckduckgo_mcp_server.server.mcp"):
            with self.assertRaises(SystemExit):
                duckduckgo_mcp_server.server.main()


class TestConfiguration(unittest.TestCase):
    def test_safe_search_enum_values(self):
        self.assertEqual(SafeSearchMode.STRICT.value, "1")
        self.assertEqual(SafeSearchMode.MODERATE.value, "-1")
        self.assertEqual(SafeSearchMode.OFF.value, "-2")

    def test_searcher_passes_safe_search_to_request(self):
        searcher = DuckDuckGoSearcher(safe_search=SafeSearchMode.STRICT)
        ctx = DummyCtx()

        mock_resp = _mock_post_response("<html><body></body></html>")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            asyncio.run(searcher.search("test", ctx))

        call_kwargs = mock_client.post.call_args
        post_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
        self.assertEqual(post_data["kp"], "1")

    def test_searcher_passes_region_to_request(self):
        searcher = DuckDuckGoSearcher(default_region="us-en")
        ctx = DummyCtx()

        mock_resp = _mock_post_response("<html><body></body></html>")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            asyncio.run(searcher.search("test", ctx))

        call_kwargs = mock_client.post.call_args
        post_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
        self.assertEqual(post_data["kl"], "us-en")
