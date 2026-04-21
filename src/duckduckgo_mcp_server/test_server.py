import asyncio
import importlib
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, patch, MagicMock
import unittest

import httpx

import duckduckgo_mcp_server.server

from duckduckgo_mcp_server.server import (
    RateLimiter,
    DuckDuckGoSearcher,
    TavilySearcher,
    SafeSearchMode,
    SearchResult,
    WebContentFetcher,
)


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

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(limiter.acquire())
            mock_sleep.assert_called_once()
            # Should wait roughly 50 seconds (60 - 10)
            wait_time = mock_sleep.call_args[0][0]
            self.assertGreater(wait_time, 40)
            self.assertLessEqual(wait_time, 60)

    def test_acquire_allows_after_window_expires(self):
        limiter = RateLimiter(requests_per_minute=2)
        limiter.requests = [
            datetime.now() - timedelta(seconds=61),
            datetime.now() - timedelta(seconds=61),
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(limiter.acquire())
            mock_sleep.assert_not_called()


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

        try:
            fetcher = WebContentFetcher()
            url = f"http://127.0.0.1:{server.server_address[1]}"
            text = asyncio.run(fetcher.fetch_and_parse(url, DummyCtx()))

            self.assertIn("Sample Heading", text)
            self.assertIn("Some meaningful paragraph.", text)
            self.assertNotIn("Navigation", text)
            self.assertNotIn("console.log", text)
        finally:
            server.shutdown()
            thread.join()

    def test_fetch_and_parse_pagination(self):
        html_content = "<html><body><p>" + "A" * 100 + "</p></body></html>"

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

        try:
            fetcher = WebContentFetcher()
            url = f"http://127.0.0.1:{server.server_address[1]}"

            # Fetch first 50 chars
            text = asyncio.run(fetcher.fetch_and_parse(url, DummyCtx(), start_index=0, max_length=50))
            self.assertIn("start_index=50 to see more", text)
            self.assertIn("of 100 total", text)

            # Fetch from offset 50
            text = asyncio.run(fetcher.fetch_and_parse(url, DummyCtx(), start_index=50, max_length=50))
            self.assertNotIn("to see more", text)
            self.assertIn("of 100 total", text)
        finally:
            server.shutdown()
            thread.join()


class TestWebContentFetcherErrors(unittest.TestCase):
    def test_fetch_returns_error_on_timeout(self):
        fetcher = WebContentFetcher()
        ctx = DummyCtx()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(fetcher.fetch_and_parse("https://example.com", ctx))
        self.assertIn("timed out", result)

    def test_fetch_returns_error_on_http_error(self):
        fetcher = WebContentFetcher()
        ctx = DummyCtx()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.request = MagicMock()
        error = httpx.HTTPStatusError("server error", request=mock_resp.request, response=mock_resp)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_resp.raise_for_status = MagicMock(side_effect=error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(fetcher.fetch_and_parse("https://example.com", ctx))
        self.assertIn("Error", result)

    def test_fetch_handles_malformed_html(self):
        fetcher = WebContentFetcher()
        ctx = DummyCtx()

        mock_resp = MagicMock()
        mock_resp.text = "<<<not valid>>>"
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(fetcher.fetch_and_parse("https://example.com", ctx))
        # Should not crash - returns some text (possibly empty or with metadata)
        self.assertIsInstance(result, str)


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


class TestTavilySearcher(unittest.TestCase):
    def _make_searcher(self):
        with patch("duckduckgo_mcp_server.server.AsyncTavilyClient"):
            searcher = TavilySearcher(api_key="test-key")
        return searcher

    def test_results_mapped_correctly(self):
        searcher = self._make_searcher()
        ctx = DummyCtx()

        mock_response = {
            "results": [
                {"title": "First", "url": "https://first.com", "content": "Snippet 1"},
                {"title": "Second", "url": "https://second.com", "content": "Snippet 2"},
            ]
        }
        searcher.client.search = AsyncMock(return_value=mock_response)

        results = asyncio.run(searcher.search("test query", ctx, max_results=10))

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "First")
        self.assertEqual(results[0].link, "https://first.com")
        self.assertEqual(results[0].snippet, "Snippet 1")
        self.assertEqual(results[0].position, 1)
        self.assertEqual(results[1].title, "Second")
        self.assertEqual(results[1].position, 2)

    def test_empty_results(self):
        searcher = self._make_searcher()
        ctx = DummyCtx()

        searcher.client.search = AsyncMock(return_value={"results": []})

        results = asyncio.run(searcher.search("empty query", ctx))

        self.assertEqual(results, [])

    def test_api_error_returns_empty(self):
        searcher = self._make_searcher()
        ctx = DummyCtx()

        searcher.client.search = AsyncMock(side_effect=Exception("API error"))

        results = asyncio.run(searcher.search("bad query", ctx))

        self.assertEqual(results, [])

    def test_format_results_for_llm(self):
        searcher = self._make_searcher()
        results = [
            SearchResult(title="Result", link="https://example.com", snippet="A snippet", position=1),
        ]

        formatted = searcher.format_results_for_llm(results)

        self.assertIn("Found 1 search results", formatted)
        self.assertIn("1. Result", formatted)
        self.assertIn("URL: https://example.com", formatted)

    def test_format_results_for_llm_empty(self):
        searcher = self._make_searcher()

        formatted = searcher.format_results_for_llm([])

        self.assertIn("No results were found", formatted)

    def test_respects_max_results(self):
        searcher = self._make_searcher()
        ctx = DummyCtx()

        # Tavily API respects max_results, so mock returns only 3 results
        mock_response = {
            "results": [
                {"title": f"R{i}", "url": f"https://r{i}.com", "content": f"S{i}"}
                for i in range(3)
            ]
        }
        searcher.client.search = AsyncMock(return_value=mock_response)

        results = asyncio.run(searcher.search("test", ctx, max_results=3))

        self.assertEqual(len(results), 3)
        # Verify max_results was passed to the Tavily API
        searcher.client.search.assert_called_once_with(
            query="test", max_results=3, search_depth="basic"
        )


class TestProviderSelection(unittest.TestCase):
    def test_provider_tavily_with_key(self):
        env = {"SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=False):
            importlib.reload(duckduckgo_mcp_server.server)
            self.assertEqual(type(duckduckgo_mcp_server.server.searcher).__name__, "TavilySearcher")

    def test_provider_duckduckgo_explicit(self):
        env = {"SEARCH_PROVIDER": "duckduckgo"}
        with patch.dict(os.environ, env, clear=False):
            importlib.reload(duckduckgo_mcp_server.server)
            self.assertEqual(type(duckduckgo_mcp_server.server.searcher).__name__, "DuckDuckGoSearcher")

    def test_provider_auto_without_tavily_key(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "", "SEARCH_PROVIDER": "auto"}, clear=False):
            importlib.reload(duckduckgo_mcp_server.server)
            self.assertEqual(type(duckduckgo_mcp_server.server.searcher).__name__, "DuckDuckGoSearcher")
