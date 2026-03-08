# CLAUDE.md

This file provides guidance for AI assistants working with the DuckDuckGo MCP Server codebase.

## Project Overview

A Model Context Protocol (MCP) server that exposes DuckDuckGo web search and webpage content fetching as MCP tools. It is built with Python using the FastMCP framework and published to PyPI as `duckduckgo-mcp-server`.

## Repository Structure

```
duckduckgo-mcp-server/
├── src/
│   └── duckduckgo_mcp_server/
│       ├── __init__.py          # Package version (0.1.1)
│       └── server.py            # Entire server implementation (249 lines)
├── .github/
│   └── workflows/
│       └── python-publish.yml   # PyPI publish on GitHub release
├── Dockerfile                   # Python 3.11-alpine image for containerized use
├── smithery.yaml                # Smithery MCP registry configuration
├── pyproject.toml               # Project metadata and dependencies
├── uv.lock                      # Locked dependency versions (committed)
├── .python-version              # Specifies Python 3.13.2 for local dev
├── LICENSE                      # MIT
└── README.md                    # User-facing installation and usage docs
```

All server logic lives in a single file: `src/duckduckgo_mcp_server/server.py`.

## Architecture

The server is composed of four components defined in `server.py`:

### `SearchResult` (dataclass, line 15)
Plain data container for a single search result with fields: `title`, `link`, `snippet`, `position`.

### `RateLimiter` (class, line 23)
Sliding-window rate limiter. Tracks timestamps of recent requests and `await`s when the per-minute limit is reached. Instantiated separately for searcher (30 req/min) and fetcher (20 req/min).

### `DuckDuckGoSearcher` (class, line 44)
- POSTs to `https://html.duckduckgo.com/html` (no API key required)
- Parses HTML response with BeautifulSoup using CSS selectors (`.result`, `.result__title`, `.result__snippet`)
- Filters ads by checking for `y.js` in URL
- Decodes DuckDuckGo redirect URLs via the `uddg` query parameter
- Formats results as numbered, LLM-friendly plain text

### `WebContentFetcher` (class, line 148)
- GETs any URL with `follow_redirects=True`
- Strips `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>` elements
- Normalizes whitespace with `re.sub(r"\s+", " ", text)`
- Truncates output to 8000 characters with `... [content truncated]` suffix

### MCP Tools (lines 214–241)
Two tools registered with the `@mcp.tool()` decorator on the global `FastMCP("ddg-search")` instance:
- `search(query: str, max_results: int = 10) -> str`
- `fetch_content(url: str) -> str`

Both tools accept an implicit `ctx: Context` parameter used exclusively for MCP-native logging (`ctx.info()`, `ctx.error()`). Never use `print()` for logging.

## Development Setup

This project uses `uv` for dependency management.

```bash
# Install dependencies
uv sync

# Run directly
uv run python -m duckduckgo_mcp_server.server

# Run with MCP Inspector (for interactive testing)
uv run mcp dev src/duckduckgo_mcp_server/server.py

# Install locally for Claude Desktop testing
uv run mcp install src/duckduckgo_mcp_server/server.py
```

No test suite exists. Manual testing is done via the MCP Inspector or by running Claude Desktop with the server installed.

## Key Conventions

### Async/Await
All I/O is async. Always use `async with httpx.AsyncClient()` rather than a shared client instance, and always set an explicit `timeout=30.0`.

### Logging
Use the MCP `Context` object for all logging, never `print()`:
```python
await ctx.info("Informational message")
await ctx.error("Error message")
```
For unhandled exceptions, print tracebacks to `sys.stderr` only:
```python
traceback.print_exc(file=sys.stderr)
```

### Error Handling
- Catch `httpx.TimeoutException` and `httpx.HTTPError` separately before a broad `Exception` catch
- Search errors return an empty list `[]`; content fetching errors return an error string (never raises)
- Always return a user-readable message, never propagate raw exceptions to MCP tool callers

### Type Hints
Use standard Python type hints from `typing` (`List`, `Dict`, `Optional`, `Any`). The codebase targets Python ≥3.10.

### Output Formatting
Tool return values must be plain text strings formatted for LLM consumption — numbered lists with clear labels (`URL:`, `Summary:`). Avoid JSON or structured data in tool output.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `mcp[cli]` | ≥1.3.0 | FastMCP framework, MCP protocol |
| `httpx` | ≥0.28.1 | Async HTTP client |
| `beautifulsoup4` | ≥4.13.3 | HTML parsing |

Managed via `uv`. After adding/removing dependencies, update `pyproject.toml` and run `uv lock` to regenerate `uv.lock`.

## Deployment

### PyPI
Releases are triggered by publishing a GitHub Release. The `python-publish.yml` workflow builds the package with `python -m build` and publishes to PyPI using trusted publishing (no token required).

To bump the version, update it in both:
- `src/duckduckgo_mcp_server/__init__.py`
- `pyproject.toml`

### Docker
```bash
docker build -t duckduckgo-mcp-server .
docker run duckduckgo-mcp-server
```
The container runs via stdio (no port exposure needed). The Dockerfile uses Python 3.11-alpine with `gcc`, `musl-dev`, and `linux-headers` for native extension builds.

### Smithery
`smithery.yaml` configures the server for the Smithery MCP registry. It runs the server via `python -m duckduckgo_mcp_server.server` with no required configuration parameters.

### Claude Desktop
```json
{
  "mcpServers": {
    "ddg-search": {
      "command": "uvx",
      "args": ["duckduckgo-mcp-server"]
    }
  }
}
```

## Common Tasks

**Add a new MCP tool:**
1. Implement the logic as a method on an existing class or a new class in `server.py`
2. Decorate a top-level async function with `@mcp.tool()`
3. Accept `ctx: Context` as a parameter for logging
4. Return a plain text string

**Modify rate limits:**
Change the `requests_per_minute` argument when constructing `RateLimiter` instances at lines 51 and 150.

**Modify content truncation:**
The 8000-character limit is hardcoded at line 189 in `WebContentFetcher.fetch_and_parse`.

**Change search parameters:**
The POST body to DuckDuckGo is constructed at lines 77–81. The `kl` field controls region/language (empty = global default).
