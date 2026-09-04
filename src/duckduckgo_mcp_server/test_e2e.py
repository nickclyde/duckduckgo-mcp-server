"""End-to-end MCP protocol tests using in-memory client/server sessions."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mcp.client import Client

import duckduckgo_mcp_server.server as ddg_server
from duckduckgo_mcp_server.server import mcp as mcp_app


@pytest.fixture
def allow_private_fetches():
    """Let fetch_content reach the local test server (127.0.0.1) despite the SSRF guard."""
    previous = ddg_server.fetcher.allow_private_urls
    ddg_server.fetcher.allow_private_urls = True
    try:
        yield
    finally:
        ddg_server.fetcher.allow_private_urls = previous


@pytest.fixture
def ddg_html_factory():
    """Build minimal DDG-like HTML pages."""

    def _build(results):
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

    return _build


@pytest.fixture
def local_http_server():
    """Start a local HTTP server serving given HTML content."""

    servers = []

    def _make_server(html_content):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield _make_server

    for s in servers:
        s.shutdown()


@pytest.mark.asyncio
async def test_server_lists_tools():
    async with Client(mcp_app) as client:
        tools_result = await client.list_tools()
        tool_names = {t.name for t in tools_result.tools}
        assert "search" in tool_names
        assert "fetch_content" in tool_names
        assert "expand_link" in tool_names

        # Verify input schemas exist
        for tool in tools_result.tools:
            assert tool.input_schema is not None
            assert "properties" in tool.input_schema


@pytest.mark.asyncio
async def test_fetch_content_tool_e2e(local_http_server, allow_private_fetches):
    html = "<html><body><h1>Hello E2E</h1><p>Test content here.</p></body></html>"
    url = local_http_server(html)

    async with Client(mcp_app) as client:
        result = await client.call_tool("fetch_content", {"url": url})
        text = result.content[0].text
        assert "Hello E2E" in text
        assert "Test content here." in text


@pytest.mark.asyncio
async def test_search_tool_e2e(ddg_html_factory):
    html = ddg_html_factory([
        {"title": "E2E Result", "href": "https://e2e.example.com", "snippet": "An e2e snippet"},
    ])

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.text = html
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with Client(mcp_app) as client:
            result = await client.call_tool("search", {"query": "e2e test"})
            text = result.content[0].text
            assert "E2E Result" in text
            assert "https://e2e.example.com" in text


@pytest.mark.asyncio
async def test_fetch_content_tool_accepts_backend_param(local_http_server, allow_private_fetches):
    """The fetch_content tool should accept a per-call `backend` argument."""
    html = "<html><body><h1>Backend Param Test</h1></body></html>"
    url = local_http_server(html)

    async with Client(mcp_app) as client:
        result = await client.call_tool("fetch_content", {"url": url, "backend": "httpx"})
        text = result.content[0].text
        assert "Backend Param Test" in text


@pytest.mark.asyncio
async def test_fetch_content_tool_lists_backend_in_schema():
    """The `backend` parameter should be advertised in fetch_content's input schema."""
    async with Client(mcp_app) as client:
        tools_result = await client.list_tools()
        fetch_tool = next(t for t in tools_result.tools if t.name == "fetch_content")
        props = fetch_tool.input_schema.get("properties", {})
        assert "backend" in props, f"expected 'backend' in fetch_content input schema, got: {list(props)}"
        assert "parse_mode" in props, f"expected 'parse_mode' in fetch_content input schema, got: {list(props)}"


@pytest.mark.asyncio
async def test_search_tool_handles_errors():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with Client(mcp_app) as client:
            result = await client.call_tool("search", {"query": "timeout test"})
            text = result.content[0].text
            # Should return a user-friendly message, not a protocol error
            assert "No results were found" in text or "error" in text.lower()


@pytest.mark.asyncio
async def test_expand_link_tool_round_trips_ref_token():
    long_url = "https://example.com/" + "segment/" * 20 + "?q=1"
    token = ddg_server.links.shorten(long_url)

    async with Client(mcp_app) as client:
        result = await client.call_tool("expand_link", {"token": token})
        assert result.content[0].text == long_url

        missing = await client.call_tool("expand_link", {"token": "ref://00000000"})
        assert missing.content[0].text.startswith("Error: Unknown link reference")


@pytest.mark.asyncio
async def test_fetch_content_tool_accepts_ref_token(local_http_server, allow_private_fetches):
    html = "<html><body><h1>Via Token</h1></body></html>"
    url = local_http_server(html) + "/" + "p/" * 70
    token = ddg_server.links.shorten(url)

    async with Client(mcp_app) as client:
        result = await client.call_tool("fetch_content", {"url": token})
        assert "Via Token" in result.content[0].text
