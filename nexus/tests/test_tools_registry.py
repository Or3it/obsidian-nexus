"""Tests for orchestrator/tools/registry.py"""
from unittest.mock import AsyncMock, patch

from orchestrator.tools.registry import TOOLS, dispatch


class TestToolsList:
    def test_tools_contains_web_search(self) -> None:
        names = {t["name"] for t in TOOLS}
        assert "web_search" in names

    def test_tools_contains_web_fetch(self) -> None:
        names = {t["name"] for t in TOOLS}
        assert "web_fetch" in names

    def test_all_tools_have_required_fields(self) -> None:
        for tool in TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool


class TestDispatch:
    async def test_web_search_calls_run_web_search(self) -> None:
        fake_results = [{"title": "Example", "href": "https://ex.com", "body": "snippet"}]
        with patch(
            "orchestrator.tools.registry.run_web_search",
            new=AsyncMock(return_value=fake_results),
        ):
            result = await dispatch("web_search", {"query": "test"})

        assert "Example" in result
        assert "https://ex.com" in result
        assert "snippet" in result

    async def test_web_search_formats_multiple_results(self) -> None:
        fake_results = [
            {"title": "A", "href": "https://a.com", "body": "aaa"},
            {"title": "B", "href": "https://b.com", "body": "bbb"},
        ]
        with patch(
            "orchestrator.tools.registry.run_web_search",
            new=AsyncMock(return_value=fake_results),
        ):
            result = await dispatch("web_search", {"query": "multi"})

        assert "[1]" in result
        assert "[2]" in result

    async def test_web_fetch_calls_run_web_fetch(self) -> None:
        fake_result = {"url": "https://ex.com", "title": "Example", "text": "page content"}
        with patch(
            "orchestrator.tools.registry.run_web_fetch",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await dispatch("web_fetch", {"url": "https://ex.com"})

        assert "Example" in result
        assert "https://ex.com" in result
        assert "page content" in result

    async def test_web_fetch_no_title_omits_bracket_header(self) -> None:
        fake_result = {"url": "https://ex.com", "title": "", "text": "raw text"}
        with patch(
            "orchestrator.tools.registry.run_web_fetch",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await dispatch("web_fetch", {"url": "https://ex.com"})

        assert "[" not in result
        assert "raw text" in result

    async def test_unknown_tool_returns_error_string(self) -> None:
        result = await dispatch("not_a_real_tool", {})
        assert "Unknown tool" in result
        assert "not_a_real_tool" in result
