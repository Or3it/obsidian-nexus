"""Web fetch tool for the Anthropic SDK path.

Fetches a URL with httpx and extracts readable text via stdlib html.parser.
Never raises — returns an error dict on any failure.
"""

import logging
from html.parser import HTMLParser
from typing import Any

import httpx

log = logging.getLogger(__name__)

WEB_FETCH_TOOL: dict[str, Any] = {
    "name": "web_fetch",
    "description": (
        "Fetch the content of a specific web page. Use when you have a URL and need "
        "to read its text. Returns the page title and extracted readable text, "
        "truncated to ~8 000 characters."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch (http or https)",
            },
        },
        "required": ["url"],
    },
}

_USER_AGENT = "Mozilla/5.0 (compatible; Nexus/1.0)"

_SKIP_TAGS = frozenset(["script", "style", "meta", "link", "noscript", "head"])


class _TextExtractor(HTMLParser):
    """Extract visible text and <title> from an HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth: int = 0
        self._in_title: bool = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if not stripped:
            return
        if self._in_title:
            self._title_parts.append(stripped)
        elif self._skip_depth == 0:
            self._text_parts.append(stripped)

    @property
    def title(self) -> str:
        return " ".join(self._title_parts)

    @property
    def text(self) -> str:
        return " ".join(self._text_parts)


async def run_web_fetch(url: str, max_chars: int = 8_000) -> dict[str, str]:
    """Fetch a URL and return {url, title, text}. Never raises."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except Exception as exc:
        log.warning("web_fetch failed for %r: %s", url, exc)
        return {"url": url, "title": "", "text": f"Fetch failed: {exc}"}

    content_type = response.headers.get("content-type", "")
    final_url = str(response.url)

    if "html" not in content_type.lower():
        return {"url": final_url, "title": "", "text": response.text[:max_chars]}

    extractor = _TextExtractor()
    try:
        extractor.feed(response.text)
    except Exception as exc:
        log.warning("HTML parse error for %r: %s", url, exc)
        return {"url": final_url, "title": "", "text": response.text[:max_chars]}

    return {
        "url": final_url,
        "title": extractor.title,
        "text": extractor.text[:max_chars],
    }
