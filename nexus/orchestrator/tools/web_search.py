"""Web search tool for the Anthropic SDK path.

Uses DDGS (formerly duckduckgo_search; renamed to ddgs in v8+).
The synchronous DDGS call runs in a thread executor so it doesn't block
the uvicorn event loop.

Tool schema conforms to Anthropic's input_schema format (JSON Schema).
"""

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL: dict[str, Any] = {
    "name": "web_search",
    "description": (
        "Search the web for current information. Use this when you need recent facts, "
        "news, prices, or information that may have changed since your training cutoff. "
        "Returns a list of results with title, URL, and snippet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up",
            },
        },
        "required": ["query"],
    },
}


async def run_web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Execute a DuckDuckGo text search asynchronously.

    Returns a list of {title, href, body} dicts.
    Never raises — returns a single error-result on failure.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _ddg_search, query, max_results)
    except Exception as exc:
        log.warning("web_search failed for %r: %s", query, exc)
        return [{"title": "Search failed", "href": "", "body": str(exc)}]


def _ddg_search(query: str, max_results: int) -> list[dict[str, str]]:
    from ddgs import DDGS  # import deferred so the module loads without ddgs installed

    results = list(DDGS().text(query, max_results=max_results))
    return [
        {
            "title": r.get("title", ""),
            "href": r.get("href", ""),
            "body": r.get("body", ""),
        }
        for r in results
    ]
