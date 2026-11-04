# P2b-T04 — web_fetch tool + tool registry

## Goal

Add a `web_fetch` tool that fetches a URL and returns readable page text, and introduce a central tool registry (`registry.py`) that owns the full list of SDK tools and their dispatch logic. The Anthropic adapter's `_complete_stream_sdk` stops hardcoding `WEB_SEARCH_TOOL` and instead delegates to the registry.

## Dependencies

- P2b-T03 (SDK streaming path, `web_search.py`, `enable_web_tools` flag)
- `httpx` (already a dependency via pyproject.toml)
- `ddgs` (already a dependency from T03)
- stdlib `html.parser` preferred over beautifulsoup4

## Files to create

- `nexus/orchestrator/tools/web_fetch.py` — `WEB_FETCH_TOOL` schema, `_TextExtractor` (html.parser), `run_web_fetch()`
- `nexus/orchestrator/tools/registry.py` — `TOOLS: list[dict]`, `async dispatch(name, input) -> str`
- `nexus/tests/test_tools_web_fetch.py` — unit tests for web_fetch
- `nexus/tests/test_tools_registry.py` — unit tests for registry

## Files to modify

- `nexus/orchestrator/tools/__init__.py` — export `TOOLS` and `dispatch`
- `nexus/orchestrator/adapters/anthropic.py` — `_complete_stream_sdk` uses `registry.TOOLS` and `registry.dispatch`
- `nexus/tests/test_anthropic_adapter.py` — update mock target from `run_web_search` to `registry.dispatch`

## Implementation notes

### web_fetch.py

Tool schema: `{"name": "web_fetch", "description": "...", "input_schema": {"url": string}}`.

`run_web_fetch(url, max_chars=8000)` — async, never raises (returns error dict on failure). Uses `httpx.AsyncClient(follow_redirects=True, timeout=15.0)`. For non-HTML content-type, return truncated raw text. For HTML, parse with `_TextExtractor`.

`_TextExtractor(HTMLParser)` — skip `script`, `style`, `meta`, `link`, `noscript` tags; track `<title>` separately; concatenate visible text parts with spaces.

### registry.py

```python
TOOLS: list[dict[str, Any]] = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]

async def dispatch(name: str, tool_input: dict[str, Any]) -> str: ...
```

Dispatch formats results as human-readable strings (same format as the original inline code in T03's adapter).

### anthropic.py changes

Replace the inline `WEB_SEARCH_TOOL` import and `if tu.name == "web_search":` block with:

```python
from ..tools.registry import TOOLS, dispatch as dispatch_tool
# ...
tools = TOOLS
# in loop:
content = await dispatch_tool(tu.name, tu_input)
```

## Tests to write

**test_tools_web_fetch.py:**
- `_TextExtractor` extracts visible text, skips script/style, extracts title
- `run_web_fetch` with mocked httpx: HTML path returns title + text
- `run_web_fetch` with non-HTML content-type returns raw text
- `run_web_fetch` with HTTP error returns error dict (no raise)
- text is truncated at max_chars

**test_tools_registry.py:**
- `TOOLS` contains both `web_search` and `web_fetch` by name
- `dispatch("web_search", ...)` calls `run_web_search` and formats output
- `dispatch("web_fetch", ...)` calls `run_web_fetch` and formats output
- `dispatch("unknown", ...)` returns "Unknown tool: unknown"

## Definition of done

- [ ] `web_fetch.py` implemented, no bare raises
- [ ] `registry.py` implemented; `TOOLS` list has both tools
- [ ] Adapter `_complete_stream_sdk` uses registry (no direct tool imports)
- [ ] All new tests pass; existing suite still 345+ passing
- [ ] `mypy orchestrator/adapters/anthropic.py --ignore-missing-imports` clean
- [ ] Committed as `feat(phase-2b): web_fetch tool + tool registry (P2b-T04)`
