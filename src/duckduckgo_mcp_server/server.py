from mcp.server.fastmcp import FastMCP, Context
import httpx
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
import urllib.parse
import sys
import traceback
import asyncio
import argparse
from datetime import datetime, timedelta
import time
import re
import os
from enum import Enum

try:
    from tavily import TavilyClient
    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False


class SafeSearchMode(Enum):
    """DuckDuckGo SafeSearch modes"""
    STRICT = "1"      # kp=1: Strict filtering (most restrictive)
    MODERATE = "-1"   # kp=-1: Moderate filtering (default)
    OFF = "-2"        # kp=-2: No filtering


@dataclass
class SearchResult:
    title: str
    link: str
    snippet: str
    position: int


class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self.requests = []

    async def acquire(self):
        now = datetime.now()
        # Remove requests older than 1 minute
        self.requests = [
            req for req in self.requests if now - req < timedelta(minutes=1)
        ]

        if len(self.requests) >= self.requests_per_minute:
            # Wait until we can make another request
            wait_time = 60 - (now - self.requests[0]).total_seconds()
            if wait_time > 0:
                await asyncio.sleep(wait_time)

        self.requests.append(now)


class DuckDuckGoSearcher:
    BASE_URL = "https://html.duckduckgo.com/html"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    def __init__(self, safe_search: SafeSearchMode = SafeSearchMode.MODERATE, default_region: str = ""):
        """
        Initialize DuckDuckGo searcher

        Args:
            safe_search: SafeSearch filtering mode (STRICT/MODERATE/OFF) - fixed at startup
            default_region: Default region code (e.g., 'us-en', 'cn-zh', 'wt-wt' for no region)
        """
        self.rate_limiter = RateLimiter()
        self.safe_search = safe_search
        self.default_region = default_region

    def format_results_for_llm(self, results: List[SearchResult]) -> str:
        """Format results in a natural language style that's easier for LLMs to process"""
        if not results:
            return "No results were found for your search query. This could be due to DuckDuckGo's bot detection or the query returned no matches. Please try rephrasing your search or try again in a few minutes."

        output = []
        output.append(f"Found {len(results)} search results:\n")

        for result in results:
            output.append(f"{result.position}. {result.title}")
            output.append(f"   URL: {result.link}")
            output.append(f"   Summary: {result.snippet}")
            output.append("")  # Empty line between results

        return "\n".join(output)

    async def search(
        self, query: str, ctx: Context, max_results: int = 10, region: str = ""
    ) -> List[SearchResult]:
        """
        Search DuckDuckGo

        Args:
            query: Search query
            ctx: MCP context
            max_results: Maximum results to return
            region: Region code (empty = use default, or specify like 'us-en', 'cn-zh', 'jp-ja')
        """
        try:
            # Apply rate limiting
            await self.rate_limiter.acquire()

            # Use provided region or fall back to default
            effective_region = region if region else self.default_region

            # Create form data for POST request
            data = {
                "q": query,
                "b": "",
                "kl": effective_region,  # Region/language code
                "kp": self.safe_search.value,  # SafeSearch mode (fixed)
            }

            await ctx.info(f"Searching DuckDuckGo for: {query} (SafeSearch: {self.safe_search.name}, Region: {effective_region or 'default'})")

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.BASE_URL, data=data, headers=self.HEADERS, timeout=30.0
                )
                response.raise_for_status()

            # Parse HTML response
            soup = BeautifulSoup(response.text, "html.parser")
            if not soup:
                await ctx.error("Failed to parse HTML response")
                return []

            results = []
            for result in soup.select(".result"):
                title_elem = result.select_one(".result__title")
                if not title_elem:
                    continue

                link_elem = title_elem.find("a")
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                link = link_elem.get("href", "")

                # Skip ad results
                if "y.js" in link:
                    continue

                # Clean up DuckDuckGo redirect URLs
                if link.startswith("//duckduckgo.com/l/?uddg="):
                    link = urllib.parse.unquote(link.split("uddg=")[1].split("&")[0])

                snippet_elem = result.select_one(".result__snippet")
                snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                results.append(
                    SearchResult(
                        title=title,
                        link=link,
                        snippet=snippet,
                        position=len(results) + 1,
                    )
                )

                if len(results) >= max_results:
                    break

            await ctx.info(f"Successfully found {len(results)} results")
            return results

        except httpx.TimeoutException:
            await ctx.error("Search request timed out")
            return []
        except httpx.HTTPError as e:
            await ctx.error(f"HTTP error occurred: {str(e)}")
            return []
        except Exception as e:
            await ctx.error(f"Unexpected error during search: {str(e)}")
            traceback.print_exc(file=sys.stderr)
            return []


class TavilySearcher:
    """Search provider using Tavily API."""

    def __init__(self, api_key: str):
        self.client = TavilyClient(api_key=api_key)
        self.rate_limiter = RateLimiter()

    def format_results_for_llm(self, results: List[SearchResult]) -> str:
        """Format results in a natural language style that's easier for LLMs to process"""
        if not results:
            return "No results were found for your search query. Please try rephrasing your search or try again."

        output = []
        output.append(f"Found {len(results)} search results:\n")

        for result in results:
            output.append(f"{result.position}. {result.title}")
            output.append(f"   URL: {result.link}")
            output.append(f"   Summary: {result.snippet}")
            output.append("")

        return "\n".join(output)

    async def search(
        self, query: str, ctx: Context, max_results: int = 10, region: str = ""
    ) -> List[SearchResult]:
        """Search using Tavily API."""
        try:
            await self.rate_limiter.acquire()

            await ctx.info(f"Searching Tavily for: {query}")

            response = self.client.search(
                query=query,
                max_results=max_results,
                search_depth="basic",
            )

            results = []
            for i, item in enumerate(response.get("results", []), start=1):
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        link=item.get("url", ""),
                        snippet=item.get("content", ""),
                        position=i,
                    )
                )
                if len(results) >= max_results:
                    break

            await ctx.info(f"Successfully found {len(results)} results via Tavily")
            return results

        except Exception as e:
            await ctx.error(f"Tavily search error: {str(e)}")
            traceback.print_exc(file=sys.stderr)
            return []


class WebContentFetcher:
    def __init__(self):
        self.rate_limiter = RateLimiter(requests_per_minute=20)

    async def fetch_and_parse(self, url: str, ctx: Context, start_index: int = 0, max_length: int = 8000) -> str:
        """Fetch and parse content from a webpage"""
        try:
            await self.rate_limiter.acquire()

            await ctx.info(f"Fetching content from: {url}")

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    },
                    follow_redirects=True,
                    timeout=30.0,
                )
                response.raise_for_status()

            # Parse the HTML
            soup = BeautifulSoup(response.text, "html.parser")

            # Remove script and style elements
            for element in soup(["script", "style", "nav", "header", "footer"]):
                element.decompose()

            # Get the text content
            text = soup.get_text()

            # Clean up the text
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = " ".join(chunk for chunk in chunks if chunk)

            # Remove extra whitespace
            text = re.sub(r"\s+", " ", text).strip()

            total_length = len(text)

            # Apply pagination
            text = text[start_index:start_index + max_length]
            is_truncated = start_index + max_length < total_length

            # Add metadata
            metadata = f"\n\n---\n[Content info: Showing characters {start_index}-{start_index + len(text)} of {total_length} total"
            if is_truncated:
                metadata += f". Use start_index={start_index + max_length} to see more"
            metadata += "]"
            text += metadata

            await ctx.info(
                f"Successfully fetched and parsed content ({len(text)} characters)"
            )
            return text

        except httpx.TimeoutException:
            await ctx.error(f"Request timed out for URL: {url}")
            return "Error: The request timed out while trying to fetch the webpage."
        except httpx.HTTPError as e:
            await ctx.error(f"HTTP error occurred while fetching {url}: {str(e)}")
            return f"Error: Could not access the webpage ({str(e)})"
        except Exception as e:
            await ctx.error(f"Error fetching content from {url}: {str(e)}")
            return f"Error: An unexpected error occurred while fetching the webpage ({str(e)})"


# Initialize FastMCP server
mcp = FastMCP("ddg-search")

# Read configuration from environment variables
SAFE_SEARCH_MODE = os.getenv("DDG_SAFE_SEARCH", "MODERATE").upper()
REGION_CODE = os.getenv("DDG_REGION", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "auto").lower()

# Validate and set SafeSearch mode
try:
    safe_search = SafeSearchMode[SAFE_SEARCH_MODE]
except KeyError:
    print(f"Warning: Invalid DDG_SAFE_SEARCH value '{SAFE_SEARCH_MODE}', using MODERATE", file=sys.stderr)
    safe_search = SafeSearchMode.MODERATE

# Select search provider based on configuration
if SEARCH_PROVIDER == "tavily":
    if not TAVILY_AVAILABLE:
        print("Warning: tavily-python not installed. Install with: pip install 'duckduckgo-mcp-server[tavily]'", file=sys.stderr)
        print("Falling back to DuckDuckGo.", file=sys.stderr)
        searcher = DuckDuckGoSearcher(safe_search=safe_search, default_region=REGION_CODE)
    elif not TAVILY_API_KEY:
        print("Warning: SEARCH_PROVIDER=tavily but TAVILY_API_KEY not set. Falling back to DuckDuckGo.", file=sys.stderr)
        searcher = DuckDuckGoSearcher(safe_search=safe_search, default_region=REGION_CODE)
    else:
        searcher = TavilySearcher(api_key=TAVILY_API_KEY)
elif SEARCH_PROVIDER == "duckduckgo":
    searcher = DuckDuckGoSearcher(safe_search=safe_search, default_region=REGION_CODE)
else:
    # auto: use Tavily if available and configured, otherwise DuckDuckGo
    if TAVILY_AVAILABLE and TAVILY_API_KEY:
        searcher = TavilySearcher(api_key=TAVILY_API_KEY)
    else:
        searcher = DuckDuckGoSearcher(safe_search=safe_search, default_region=REGION_CODE)

fetcher = WebContentFetcher()

provider_name = "Tavily" if isinstance(searcher, TavilySearcher) else "DuckDuckGo"
print(f"DuckDuckGo MCP Server initialized:", file=sys.stderr)
print(f"  Search Provider: {provider_name}", file=sys.stderr)
if isinstance(searcher, DuckDuckGoSearcher):
    print(f"  SafeSearch: {safe_search.name} (kp={safe_search.value})", file=sys.stderr)
    print(f"  Default Region: {REGION_CODE or 'none'}", file=sys.stderr)


@mcp.tool()
async def search(query: str, ctx: Context, max_results: int = 10, region: str = "") -> str:
    """Search the web using DuckDuckGo or Tavily (configurable via SEARCH_PROVIDER env var). Returns a list of results with titles, URLs, and snippets. Use this to find current information, research topics, or locate specific websites. For best results, use specific and descriptive search queries.

    Args:
        query: The search query string. Be specific for better results (e.g., 'Python asyncio tutorial' rather than 'Python').
        max_results: Maximum number of results to return, between 1 and 20 (default: 10).
        region: Optional region/language code to localize results (DuckDuckGo only). Examples: 'us-en' (USA/English), 'uk-en' (UK/English), 'de-de' (Germany/German), 'fr-fr' (France/French), 'jp-ja' (Japan/Japanese), 'cn-zh' (China/Chinese), 'wt-wt' (no region). Leave empty to use the server default.
        ctx: MCP context for logging.
    """
    try:
        results = await searcher.search(query, ctx, max_results, region)
        return searcher.format_results_for_llm(results)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return f"An error occurred while searching: {str(e)}"


@mcp.tool()
async def fetch_content(url: str, ctx: Context, start_index: int = 0, max_length: int = 8000) -> str:
    """Fetch and extract the main text content from a webpage. Strips out navigation, headers, footers, scripts, and styles to return clean readable text. Use this after searching to read the full content of a specific result. Supports pagination for long pages via start_index and max_length.

    Args:
        url: The full URL of the webpage to fetch (must start with http:// or https://).
        start_index: Character offset to start reading from (default: 0). Use this to paginate through long content.
        max_length: Maximum number of characters to return (default: 8000). Increase for more content per request or decrease for quicker responses.
        ctx: MCP context for logging.
    """
    return await fetcher.fetch_and_parse(url, ctx, start_index, max_length)


def main():
    parser = argparse.ArgumentParser(description="DuckDuckGo MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport protocol to use (default: stdio)",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
