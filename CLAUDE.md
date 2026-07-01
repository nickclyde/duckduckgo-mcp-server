# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Model Context Protocol (MCP) server providing DuckDuckGo web search and webpage content fetching. Built with Python using the FastMCP framework. Published to PyPI as `duckduckgo-mcp-server`.

## Commands

```bash
# Install dependencies
uv sync

# Run the server
uv run duckduckgo-mcp-server

# Run with MCP Inspector (for interactive testing)
mcp dev src/duckduckgo_mcp_server/server.py

# Run all tests (unit + e2e)
uv run python -m pytest src/duckduckgo_mcp_server/ -v

# Run only unit tests
uv run python -m pytest src/duckduckgo_mcp_server/test_server.py -v

# Run only e2e MCP protocol tests
uv run python -m pytest src/duckduckgo_mcp_server/test_e2e.py -v

# Run a single test
uv run python -m pytest src/duckduckgo_mcp_server/test_server.py::TestRateLimiter::test_acquire_removes_expired_entries

# Build package
uv build
```

## Architecture

Single-module server in `src/duckduckgo_mcp_server/server.py` with three main classes:

- **`DuckDuckGoSearcher`** — Scrapes DuckDuckGo's HTML endpoint (`html.duckduckgo.com/html`) via POST requests. Parses results with BeautifulSoup. Handles SafeSearch (`kp` param) and region (`kl` param) configuration.
- **`WebContentFetcher`** — Fetches arbitrary URLs, strips non-content elements (script, style, nav, header, footer), and returns cleaned text truncated to 8000 chars.
- **`RateLimiter`** — Sliding-window rate limiter (30 req/min for search, 20 req/min for content fetching).

Two MCP tools are exposed: `search` and `fetch_content`.

## Configuration

Environment variables read at startup (not per-request):
- `DDG_SAFE_SEARCH`: `STRICT` | `MODERATE` (default) | `OFF`
- `DDG_REGION`: Region code like `us-en`, `cn-zh`, `jp-ja`, `wt-wt`
- `DDG_ALLOW_PRIVATE_URLS`: `1`/`true` to let `fetch_content` reach loopback/private/link-local/metadata addresses (default off — SSRF guard). Also settable via `--allow-private-urls`.
- `DDG_SEARCH_BACKEND`: `auto` (default) | `httpx` | `curl` — HTTP backend for the search tool. `auto` falls back to curl_cffi Chrome TLS impersonation when DuckDuckGo returns a fingerprint block (HTTP 202/403); `curl`/fallback need the `[browser]` extra. Also settable via `--search-backend`.

## Testing

- **Unit tests** (`test_server.py`): 21 tests using `unittest` style with `unittest.mock.patch` to mock httpx. Covers rate limiter, search parsing, content fetching errors, and configuration.
- **E2E tests** (`test_e2e.py`): 4 tests using `pytest-asyncio` with MCP SDK's `create_connected_server_and_client_session` from `mcp.shared.memory` for in-memory MCP client/server testing.
- **CI**: GitHub Actions (`.github/workflows/test.yml`) runs tests on Python 3.10–3.14 using `astral-sh/setup-uv`.

## Key Dependencies

- `mcp[cli]>=1.26.0` (FastMCP framework)
- `httpx>=0.28.1` + `httpcore>=1.0.8` (async HTTP client; httpcore 1.0.8+ required for Python 3.14)
- `beautifulsoup4` (HTML parsing)
- Dev: `pytest`, `pytest-asyncio`, `anyio`
- Build system: `hatchling`
- Package manager: `uv`
- Python: `>=3.10`, tested through `3.14`
