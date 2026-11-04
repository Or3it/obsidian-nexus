"""Tests for orchestrator/adapters/google.py — all SDK calls are mocked."""
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import google.genai.errors as ge_errors
import pytest
from google.genai import types as genai_types

from orchestrator.adapters.base import (
    AdapterContextOverflow,
    AdapterQuotaExhausted,
    AdapterRateLimit,
    AdapterRefusal,
    AdapterTimeout,
    AdapterUnavailable,
)
from orchestrator.adapters.google import GoogleAdapter
from orchestrator.types import NexusMessage, NexusRequest, StyleProfile

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(status: str, message: str, code: int = 429) -> ge_errors.ClientError:
    """Construct a ge_errors.ClientError with the same shape the SDK produces."""
    return ge_errors.ClientError(
        code,
        {"error": {"code": code, "status": status, "message": message}},
    )


def _make_server_error(message: str) -> ge_errors.ServerError:
    """Construct a ge_errors.ServerError."""
    return ge_errors.ServerError(
        503,
        {"error": {"code": 503, "status": "UNAVAILABLE", "message": message}},
    )


def _make_request(*contents: str) -> NexusRequest:
    messages = [NexusMessage(role="user", content=c) for c in contents]
    return NexusRequest(
        thread_id="test-thread",
        messages=messages,
        style_profile=StyleProfile(),
    )


def _make_chunk(text: str, finish_reason: Any = None) -> MagicMock:
    """Build a mock GenerateContentResponse chunk."""
    part = MagicMock()
    part.text = text
    candidate = MagicMock()
    candidate.content.parts = [part]
    candidate.finish_reason = finish_reason
    chunk = MagicMock()
    chunk.candidates = [candidate]
    return chunk


async def _async_stream(chunks: list[MagicMock]) -> AsyncIterator[MagicMock]:
    for chunk in chunks:
        yield chunk


def _fixture_chunks() -> list[MagicMock]:
    """Return mock chunks matching tests/fixtures/google_stream.json."""
    raw = json.loads((FIXTURES / "google_stream.json").read_text())
    result = []
    for c in raw["chunks"]:
        fr = genai_types.FinishReason.STOP if c["finish_reason"] == "STOP" else None
        result.append(_make_chunk(c["text"], finish_reason=fr))
    return result


def _make_adapter(context_window: int = 1_048_576) -> tuple[GoogleAdapter, MagicMock]:
    """Create a GoogleAdapter backed by a mock client. Returns (adapter, mock_client)."""
    with patch.dict("os.environ", {"GOOGLE_AI_STUDIO_API_KEY": "test-key"}):
        with patch("google.genai.Client"):
            adapter = GoogleAdapter(context_window=context_window)

    mock_client = MagicMock()
    # Default: count_tokens returns a small token count
    count_result = MagicMock()
    count_result.total_tokens = 10
    mock_client.models.count_tokens.return_value = count_result
    mock_client.aio.models.count_tokens = AsyncMock(return_value=count_result)

    adapter._client = mock_client
    return adapter, mock_client


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_missing_api_key_raises_value_error(self) -> None:
        """Constructing without the env var raises ValueError immediately."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="GOOGLE_AI_STUDIO_API_KEY"):
                GoogleAdapter()

    def test_custom_api_key_env_name(self) -> None:
        """Constructor uses the env var name passed as api_key_env."""
        with patch.dict("os.environ", {"MY_KEY": "abc"}):
            with patch("google.genai.Client"):
                adapter = GoogleAdapter(api_key_env="MY_KEY")
        assert adapter.model == "gemini-3.1-pro-preview"


# ---------------------------------------------------------------------------
# complete_stream
# ---------------------------------------------------------------------------


class TestCompleteStream:
    async def test_yields_tokens_in_order_from_fixture(self) -> None:
        """complete_stream yields text tokens from the fixture then a final token."""
        adapter, mock_client = _make_adapter()
        chunks = _fixture_chunks()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_stream(chunks)
        )

        tokens = [t async for t in adapter.complete_stream(_make_request("hello"))]

        text_tokens = [t for t in tokens if not t.is_final]
        final_tokens = [t for t in tokens if t.is_final]
        assert [t.text for t in text_tokens] == ["Hello, how are you today", "?"]
        assert len(final_tokens) == 1
        assert final_tokens[0].text == ""

    async def test_quota_429_raises_adapter_quota_exhausted(self) -> None:
        """HTTP 429 with PerDay quota ID raises AdapterQuotaExhausted."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=_make_client_error(
                "RESOURCE_EXHAUSTED",
                "GenerateRequestsPerDayPerProjectPerModel-FreeTier quota exceeded",
            )
        )

        with pytest.raises(AdapterQuotaExhausted, match="daily quota"):
            async for _ in adapter.complete_stream(_make_request("hi")):
                pass

    async def test_rate_limit_429_raises_adapter_rate_limit(self) -> None:
        """HTTP 429 with PerMinute quota ID (only) raises AdapterRateLimit."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=_make_client_error(
                "RESOURCE_EXHAUSTED",
                "GenerateRequestsPerMinutePerProjectPerModel-FreeTier exceeded",
            )
        )

        with pytest.raises(AdapterRateLimit, match="rate limited"):
            async for _ in adapter.complete_stream(_make_request("hi")):
                pass

    async def test_server_error_5xx_raises_adapter_unavailable(self) -> None:
        """HTTP 5xx ServerError raises AdapterUnavailable."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=_make_server_error("Service Unavailable")
        )

        with pytest.raises(AdapterUnavailable, match="server error"):
            async for _ in adapter.complete_stream(_make_request("hi")):
                pass

    async def test_safety_block_raises_adapter_refusal(self) -> None:
        """A chunk with finish_reason=SAFETY raises AdapterRefusal."""
        adapter, mock_client = _make_adapter()
        safety_chunk = _make_chunk("", finish_reason=genai_types.FinishReason.SAFETY)
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_stream([safety_chunk])
        )

        with pytest.raises(AdapterRefusal, match="safety"):
            async for _ in adapter.complete_stream(_make_request("hi")):
                pass

    async def test_context_overflow_raises_before_api_call(self) -> None:
        """Prompt exceeding 90% of context_window raises AdapterContextOverflow."""
        adapter, mock_client = _make_adapter(context_window=5)
        # count_tokens returns 100 tokens for a 5-token window → overflow
        count_result = MagicMock()
        count_result.total_tokens = 100
        mock_client.aio.models.count_tokens = AsyncMock(return_value=count_result)

        with pytest.raises(AdapterContextOverflow, match="tokens"):
            async for _ in adapter.complete_stream(_make_request("hi")):
                pass

        # API should NOT have been called
        mock_client.aio.models.generate_content_stream.assert_not_called()

    async def test_timeout_raises_adapter_timeout(self) -> None:
        """asyncio.TimeoutError from wait_for raises AdapterTimeout."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=TimeoutError
        )

        with pytest.raises(AdapterTimeout, match="timed out"):
            async for _ in adapter.complete_stream(_make_request("hi")):
                pass


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


class TestComplete:
    async def test_content_equals_concatenated_stream_text(self) -> None:
        """complete() joins text tokens into NexusResponse.content."""
        adapter, mock_client = _make_adapter()
        chunks = _fixture_chunks()
        mock_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_stream(chunks)
        )
        mock_client.models.count_tokens.return_value.total_tokens = 5

        response = await adapter.complete(_make_request("hello"))

        assert response.content == "Hello, how are you today?"
        assert response.model_id == "gemini-3.1-pro-preview"
        assert response.auth_mode == "api_key_free_tier"
        assert response.finish_reason == "stop"


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_returns_sdk_token_count(self) -> None:
        """count_tokens returns the SDK's total_tokens value."""
        adapter, mock_client = _make_adapter()
        mock_client.models.count_tokens.return_value.total_tokens = 42

        assert adapter.count_tokens("some text") == 42

    def test_falls_back_to_char_ratio_on_sdk_failure(self) -> None:
        """count_tokens falls back to char-ratio when the SDK call raises."""
        adapter, mock_client = _make_adapter()
        mock_client.models.count_tokens.side_effect = Exception("network error")

        text = "hello world"
        result = adapter.count_tokens(text)
        expected = max(1, int(len(text) / 3.5))
        assert result == expected

    def test_available_context_returns_configured_window(self) -> None:
        """available_context returns the context_window from init."""
        adapter, _ = _make_adapter(context_window=500_000)
        assert adapter.available_context() == 500_000


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    async def test_healthy_when_api_responds(self) -> None:
        """health_check returns healthy=True when generate_content succeeds."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content = AsyncMock(return_value=MagicMock())

        status = await adapter.health_check()

        assert status.healthy is True

    async def test_unhealthy_when_api_raises(self) -> None:
        """health_check returns healthy=False when generate_content raises."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=Exception("connection refused")
        )

        status = await adapter.health_check()

        assert status.healthy is False
        assert "connection refused" in status.detail

    async def test_caches_result_for_five_minutes(self) -> None:
        """health_check returns cached result without calling the API again."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content = AsyncMock(return_value=MagicMock())

        first = await adapter.health_check()
        second = await adapter.health_check()

        assert first is second
        assert mock_client.aio.models.generate_content.call_count == 1

    async def test_cache_expires_after_ttl(self) -> None:
        """health_check re-probes after the 5-minute TTL expires."""
        adapter, mock_client = _make_adapter()
        mock_client.aio.models.generate_content = AsyncMock(return_value=MagicMock())

        await adapter.health_check()
        # Backdate the expiry
        assert adapter._health_cache_until is not None
        adapter._health_cache_until = datetime.now(UTC) - timedelta(seconds=1)

        await adapter.health_check()

        assert mock_client.aio.models.generate_content.call_count == 2


# ---------------------------------------------------------------------------
# _build_contents
# ---------------------------------------------------------------------------


class TestBuildContents:
    def test_user_message_maps_to_user_role(self) -> None:
        adapter, _ = _make_adapter()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="Hello")],
            style_profile=StyleProfile(),
        )
        contents, system = adapter._build_contents(request)
        assert system is None
        assert len(contents) == 1
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "Hello"

    def test_assistant_message_maps_to_model_role(self) -> None:
        adapter, _ = _make_adapter()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="assistant", content="Hi!")],
            style_profile=StyleProfile(),
        )
        contents, _ = adapter._build_contents(request)
        assert contents[0].role == "model"

    def test_system_message_extracted_as_instruction(self) -> None:
        adapter, _ = _make_adapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="system", content="Be concise."),
                NexusMessage(role="user", content="Hi"),
            ],
            style_profile=StyleProfile(),
        )
        contents, system = adapter._build_contents(request)
        assert system == "Be concise."
        assert len(contents) == 1

    def test_multiple_system_messages_joined(self) -> None:
        adapter, _ = _make_adapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="system", content="Rule 1."),
                NexusMessage(role="system", content="Rule 2."),
                NexusMessage(role="user", content="Go"),
            ],
            style_profile=StyleProfile(),
        )
        _, system = adapter._build_contents(request)
        assert system == "Rule 1.\n\nRule 2."


# ---------------------------------------------------------------------------
# _classify_client_error
# ---------------------------------------------------------------------------


class TestClassifyClientError:
    def test_per_day_quota_maps_to_quota_exhausted(self) -> None:
        err = _make_client_error(
            "RESOURCE_EXHAUSTED",
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier exceeded",
        )
        result = GoogleAdapter._classify_client_error(err)
        assert isinstance(result, AdapterQuotaExhausted)

    def test_per_minute_only_maps_to_rate_limit(self) -> None:
        err = _make_client_error(
            "RESOURCE_EXHAUSTED",
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier exceeded",
        )
        result = GoogleAdapter._classify_client_error(err)
        assert isinstance(result, AdapterRateLimit)

    def test_auth_error_maps_to_unavailable(self) -> None:
        err = _make_client_error("PERMISSION_DENIED", "API key invalid", code=403)
        result = GoogleAdapter._classify_client_error(err)
        assert isinstance(result, AdapterUnavailable)
