"""Central registry for all Anthropic SDK tools.

Owns the tool schema list (TOOLS) and the async dispatch function.
The Anthropic adapter imports both; individual tool modules are not
imported directly by the adapter.
"""

from typing import Any

from .web_fetch import WEB_FETCH_TOOL, run_web_fetch
from .web_search import WEB_SEARCH_TOOL, run_web_search

TOOLS: list[dict[str, Any]] = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]


async def dispatch(name: str, tool_input: dict[str, Any]) -> str:
    """Execute a tool by name and return a human-readable result string."""
    if name == "web_search":
        results = await run_web_search(tool_input.get("query", ""))
        return "\n".join(
            f"[{i + 1}] {r['title']}\n{r['href']}\n{r['body']}"
            for i, r in enumerate(results)
        )
    if name == "web_fetch":
        result = await run_web_fetch(tool_input.get("url", ""))
        header = f"[{result['title']}]\n{result['url']}" if result["title"] else result["url"]
        return f"{header}\n\n{result['text']}"
    return f"Unknown tool: {name}"
