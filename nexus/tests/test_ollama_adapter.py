"""Tests for orchestrator/adapters/ollama.py — all HTTP calls mocked via respx."""
import json
from pathlib import Path

import httpx
import pytest
import respx

from orchestrator.adapters.base import (
    AdapterContextOverflow,
    AdapterTimeout,
    AdapterUnavailable,
)
from orchestrator.adapters.ollama import OllamaAdapter
from orchestrator.types import NexusMessage, NexusRequest, StyleProfile

FIXTURES = Path(__file__).parent / "fixtures"

_CHAT_URL = "http://localhost:11434/api/chat"
_TAGS_URL = "http://localhost:11434/api/tags"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(model: str = "gemma4:latest", **kwargs: object) -> OllamaAdapter:
    return OllamaAdapter(model=model, **kwargs)  # type: ignore[arg-type]


def _make_request(content: str = "hi") -> NexusRequest:
    return NexusRequest(
        thread_id="test-thread",
        messages=[NexusMessage(role="user", content=content)],
        style_profile=StyleProfile(),
    )


def _ndjson(*chunks: dict) -> bytes:  # type: ignore[type-arg]
    return b"\n".join(json.dumps(c).encode() for c in chunks) + b"\n"


def _fixture_bytes() -> bytes:
    return (FIXTURES / "ollama_stream.txt").read_text().encode()


def _tags_response(model_names: list[str]) -> httpx.Response:
    models = [{"name": n, "model": n} for n in model_names]
    return httpx.Response(200, json={"models": models})


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults(self) -> None:
        a = _make_adapter()
        assert a.model == "gemma4:latest"
        assert a.base_url == "http://localhost:11434"
        assert a.keep_alive == -1

    def test_trailing_slash_stripped_from_base_url(self) -> None:
        a = OllamaAdapter(model="m", base_url="http://localhost:11434/")
        assert a.base_url == "http://localhost:11434"


# ---------------------------------------------------------------------------
# complete_stream — happy path
# ---------------------------------------------------------------------------


class TestCompleteStream:
    @respx.mock
    async def test_yields_tokens_in_order_from_fixture(self) -> None:
        """complete_stream yields text tokens from the fixture then a final token."""
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(200, content=_fixture_bytes()))
        tokens = [t async for t in _make_adapter().complete_stream(_make_request())]

        text_tokens = [t for t in tokens if not t.is_final]
        final_tokens = [t for t in tokens if t.is_final]

        assert [t.text for t in text_tokens] == ["Hello", ",", " how"]
        assert len(final_tokens) == 1
        assert final_tokens[0].text == ""

    @respx.mock
    async def test_empty_content_chunks_are_skipped(self) -> None:
        """Intermediate chunks with content="" are not yielded as tokens."""
        body = _ndjson(
            {"message": {"role": "assistant", "content": ""}, "done": False},
            {"message": {"role": "assistant", "content": "hi"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        )
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(200, content=body))
        tokens = [t async for t in _make_adapter().complete_stream(_make_request())]

        text_tokens = [t for t in tokens if not t.is_final]
        assert [t.text for t in text_tokens] == ["hi"]

    # ---------------------------------------------------------------------------
    # Error classification
    # ---------------------------------------------------------------------------

    @respx.mock
    async def test_connection_refused_raises_adapter_unavailable(self) -> None:
        """ConnectError raises AdapterUnavailable mentioning 'ollama serve'."""
        respx.post(_CHAT_URL).mock(side_effect=httpx.ConnectError("Connection refused"))
        with pytest.raises(AdapterUnavailable, match="ollama serve"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_http_404_raises_adapter_unavailable_with_pull_hint(self) -> None:
        """HTTP 404 raises AdapterUnavailable mentioning 'ollama pull'."""
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(AdapterUnavailable, match="ollama pull"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_http_5xx_raises_adapter_unavailable(self) -> None:
        """HTTP 5xx raises AdapterUnavailable."""
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(AdapterUnavailable, match="503"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_context_overflow_error_chunk_raises_adapter_context_overflow(self) -> None:
        """Streamed error chunk with 'context' in message raises AdapterContextOverflow."""
        body = _ndjson({"error": "context length exceeded, input too long"})
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(200, content=body))
        with pytest.raises(AdapterContextOverflow, match="context"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_too_long_error_chunk_raises_adapter_context_overflow(self) -> None:
        """Streamed error with 'too long' also raises AdapterContextOverflow."""
        body = _ndjson({"error": "prompt is too long for this model"})
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(200, content=body))
        with pytest.raises(AdapterContextOverflow, match="too long"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_generic_error_chunk_raises_adapter_unavailable(self) -> None:
        """Streamed error chunk without context/overflow keywords raises AdapterUnavailable."""
        body = _ndjson({"error": "model failed to respond"})
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(200, content=body))
        with pytest.raises(AdapterUnavailable, match="failed to respond"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_timeout_raises_adapter_timeout(self) -> None:
        """httpx.TimeoutException raises AdapterTimeout."""
        respx.post(_CHAT_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(AdapterTimeout):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_non_200_non_404_non_5xx_raises_adapter_unavailable(self) -> None:
        """HTTP 400 raises AdapterUnavailable with status code in message."""
        respx.post(_CHAT_URL).mock(
            return_value=httpx.Response(400, content=b"bad request")
        )
        with pytest.raises(AdapterUnavailable, match="400"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


class TestComplete:
    @respx.mock
    async def test_content_equals_concatenated_stream_text(self) -> None:
        """complete() joins text tokens into NexusResponse.content."""
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(200, content=_fixture_bytes()))
        response = await _make_adapter().complete(_make_request())

        assert response.content == "Hello, how"
        assert response.model_id == "ollama:gemma4:latest"
        assert response.auth_mode == "local"
        assert response.finish_reason == "stop"

    @respx.mock
    async def test_final_token_not_included_in_content(self) -> None:
        """The is_final Token(text='') must not appear in NexusResponse.content."""
        body = _ndjson(
            {"message": {"role": "assistant", "content": "pong"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        )
        respx.post(_CHAT_URL).mock(return_value=httpx.Response(200, content=body))
        response = await _make_adapter().complete(_make_request())
        assert response.content == "pong"


# ---------------------------------------------------------------------------
# count_tokens / available_context / estimated_cost
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_hello_returns_sensible_count(self) -> None:
        """'hello' (5 chars / 3.9) → 1 (clamped to minimum 1)."""
        a = _make_adapter()
        assert a.count_tokens("hello") == max(1, int(5 / 3.9))

    def test_empty_string_returns_1(self) -> None:
        assert _make_adapter().count_tokens("") == 1

    def test_longer_text(self) -> None:
        text = "The quick brown fox jumps over the lazy dog"
        a = _make_adapter()
        assert a.count_tokens(text) == max(1, int(len(text) / 3.9))

    def test_available_context_returns_configured_window(self) -> None:
        a = OllamaAdapter(model="m", context_window=64_000)
        assert a.available_context() == 64_000

    def test_estimated_cost_is_zero(self) -> None:
        a = _make_adapter()
        cost = a.estimated_cost(_make_request())
        assert cost["usd"] == 0.0
        assert cost["pro_quota_pct"] == 0.0


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @respx.mock
    async def test_healthy_when_model_is_in_tags_list(self) -> None:
        """health_check returns healthy=True when the model is in /api/tags."""
        respx.get(_TAGS_URL).mock(
            return_value=_tags_response(["gemma4:latest", "qwen2.5-coder:7b"])
        )
        status = await _make_adapter("gemma4:latest").health_check()
        assert status.healthy is True
        assert status.detail == ""

    @respx.mock
    async def test_unhealthy_when_model_not_in_tags_list(self) -> None:
        """health_check returns healthy=False when the configured model isn't pulled."""
        respx.get(_TAGS_URL).mock(
            return_value=_tags_response(["qwen2.5-coder:7b"])
        )
        status = await _make_adapter("gemma4:latest").health_check()
        assert status.healthy is False
        assert "ollama pull" in status.detail

    @respx.mock
    async def test_unhealthy_when_api_tags_returns_non_200(self) -> None:
        """health_check returns healthy=False on non-200 from /api/tags."""
        respx.get(_TAGS_URL).mock(return_value=httpx.Response(500))
        status = await _make_adapter().health_check()
        assert status.healthy is False
        assert "500" in status.detail

    @respx.mock
    async def test_unhealthy_when_ollama_not_running(self) -> None:
        """health_check returns healthy=False on ConnectError."""
        respx.get(_TAGS_URL).mock(side_effect=httpx.ConnectError("refused"))
        status = await _make_adapter().health_check()
        assert status.healthy is False
        assert "unreachable" in status.detail

    @respx.mock
    async def test_unhealthy_when_timeout(self) -> None:
        """health_check returns healthy=False on timeout."""
        respx.get(_TAGS_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        status = await _make_adapter().health_check()
        assert status.healthy is False

    @respx.mock
    async def test_base_name_match_without_tag(self) -> None:
        """health_check matches 'gemma4' prefix when model is 'gemma4:latest'."""
        respx.get(_TAGS_URL).mock(
            return_value=_tags_response(["gemma4:7b"])  # different tag, same base name
        )
        status = await _make_adapter("gemma4:latest").health_check()
        # base_name is "gemma4", "gemma4:7b" starts with "gemma4:" → match
        assert status.healthy is True

    @respx.mock
    async def test_exact_model_name_match(self) -> None:
        """health_check matches when self.model exactly equals a listed name."""
        respx.get(_TAGS_URL).mock(
            return_value=_tags_response(["gemma4:latest"])
        )
        status = await _make_adapter("gemma4:latest").health_check()
        assert status.healthy is True
