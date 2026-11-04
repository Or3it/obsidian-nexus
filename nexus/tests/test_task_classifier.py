"""Tests for orchestrator.heuristics.task_classifier."""

from unittest.mock import AsyncMock

from orchestrator.heuristics.task_classifier import (
    classify_heuristic,
    classify_ollama,
)

# ---------------------------------------------------------------------------
# classify_heuristic
# ---------------------------------------------------------------------------


class TestClassifyHeuristic:
    def test_code_fence_is_coding_high(self) -> None:
        result = classify_heuristic("Can you fix this?\n```python\nprint('hi')\n```")
        assert result.task_class == "coding"
        assert result.confidence == "high"

    def test_traceback_is_coding(self) -> None:
        result = classify_heuristic("I see: Traceback (most recent call last): File ...")
        assert result.task_class == "coding"

    def test_py_extension_is_coding(self) -> None:
        result = classify_heuristic("What does helper.py do?")
        assert result.task_class == "coding"

    def test_def_keyword_is_coding(self) -> None:
        result = classify_heuristic("def my_function(x): return x * 2")
        assert result.task_class == "coding"

    def test_refactor_keyword_is_coding(self) -> None:
        result = classify_heuristic("Can you refactor this loop for clarity?")
        assert result.task_class == "coding"

    def test_two_question_marks_is_research(self) -> None:
        result = classify_heuristic("Can you compare X and Y? Why is one faster?")
        assert result.task_class == "research"

    def test_explain_keyword_is_research(self) -> None:
        result = classify_heuristic("Please explain the history of async programming.")
        assert result.task_class == "research"

    def test_analyze_keyword_is_research(self) -> None:
        result = classify_heuristic("Please analyze the tradeoffs between A and B.")
        assert result.task_class == "research"

    def test_long_message_over_400_chars_is_research(self) -> None:
        result = classify_heuristic("word " * 85)  # 425 chars
        assert result.task_class == "research"

    def test_short_greeting_is_conversational(self) -> None:
        result = classify_heuristic("Hello!")
        assert result.task_class == "conversational"
        assert result.confidence == "medium"

    def test_short_message_under_120_chars_is_conversational(self) -> None:
        result = classify_heuristic("What time is it?")
        assert result.task_class == "conversational"

    def test_bland_medium_statement_is_general_low(self) -> None:
        # 120–400 chars, no keywords, no code markers
        result = classify_heuristic("Let me know what you think about this approach please. " * 3)
        assert result.task_class == "general"
        assert result.confidence == "low"


# ---------------------------------------------------------------------------
# classify_ollama
# ---------------------------------------------------------------------------


class TestClassifyOllama:
    async def test_coding_label_returned(self) -> None:
        adapter = AsyncMock()
        adapter.complete_once = AsyncMock(return_value="coding")
        result = await classify_ollama("def foo(): pass", adapter)
        assert result.task_class == "coding"
        assert result.confidence == "medium"
        assert result.reason == "ollama-classified"

    async def test_research_label_returned(self) -> None:
        adapter = AsyncMock()
        adapter.complete_once = AsyncMock(return_value="research")
        result = await classify_ollama("Explain the tradeoffs", adapter)
        assert result.task_class == "research"

    async def test_conversational_label_returned(self) -> None:
        adapter = AsyncMock()
        adapter.complete_once = AsyncMock(return_value="conversational")
        result = await classify_ollama("Hey!", adapter)
        assert result.task_class == "conversational"

    async def test_general_label_returned(self) -> None:
        adapter = AsyncMock()
        adapter.complete_once = AsyncMock(return_value="general")
        result = await classify_ollama("Do the thing", adapter)
        assert result.task_class == "general"

    async def test_garbage_falls_back_to_general(self) -> None:
        adapter = AsyncMock()
        adapter.complete_once = AsyncMock(return_value="gibberish_output!!!")
        result = await classify_ollama("some message", adapter)
        assert result.task_class == "general"

    async def test_empty_response_falls_back_to_general(self) -> None:
        adapter = AsyncMock()
        adapter.complete_once = AsyncMock(return_value="   ")
        result = await classify_ollama("anything", adapter)
        assert result.task_class == "general"

    async def test_label_with_trailing_whitespace_accepted(self) -> None:
        adapter = AsyncMock()
        adapter.complete_once = AsyncMock(return_value="  coding  ")
        result = await classify_ollama("def foo(): pass", adapter)
        assert result.task_class == "coding"
