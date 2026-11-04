"""Tests for orchestrator/adapters/openrouter.py — all HTTP calls mocked via respx."""
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from orchestrator.adapters.base import (
    AdapterModelUnavailable,
    AdapterQuotaExhausted,
    AdapterRateLimit,
    AdapterRefusal,
    AdapterTimeout,
    AdapterUnavailable,
)
from orchestrator.adapters.openrouter import (
    OPENROUTER_API_BASE,
    OpenRouterAdapter,
    _extract_error,
)
from orchestrator.types import NexusMessage, NexusRequest, StyleProfile

FIXTURES = Path(__file__).parent / "fixtures"

_COMPLETIONS_URL = f"{OPENROUTER_API_BASE}/chat/completions"
_MODELS_URL = f"{OPENROUTER_API_BASE}/models"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    model: str = "test/model:free",
    fallback_models: list[str] | None = None,
) -> OpenRouterAdapter:
    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
        return OpenRouterAdapter(model=model, fallback_models=fallback_models)


def _make_request(content: str = "hi") -> NexusRequest:
    return NexusRequest(
        thread_id="test-thread",
        messages=[NexusMessage(role="user", content=content)],
        style_profile=StyleProfile(),
    )


def _sse_fixture() -> bytes:
    return (FIXTURES / "openrouter_stream.txt").read_text().encode()


def _sse_error(message: str, code: int = 429) -> bytes:
    import json

    return json.dumps({"error": {"message": message, "code": code}}).encode()


def _sse_inline_error(message: str, code: int) -> bytes:
    import json

    line = json.dumps({"error": {"message": message, "code": code}})
    return f"data: {line}\n\n".encode()


def _sse_content_filter() -> bytes:
    return (
        b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"content_filter"}]}\n\n'
        b"data: [DONE]\n"
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_missing_api_key_raises_value_error(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
                OpenRouterAdapter(model="test/model:free")

    def test_custom_api_key_env_name(self) -> None:
        with patch.dict("os.environ", {"MY_OR_KEY": "sk-abc"}):
            adapter = OpenRouterAdapter(model="test/model:free", api_key_env="MY_OR_KEY")
        assert adapter.model == "test/model:free"


# ---------------------------------------------------------------------------
# complete_stream — happy path
# ---------------------------------------------------------------------------


class TestCompleteStream:
    @respx.mock
    async def test_yields_content_tokens_skips_reasoning(self) -> None:
        """Fixture has a reasoning chunk + content chunk. Only content is yielded."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=_sse_fixture())
        )
        adapter = _make_adapter()
        tokens = [t async for t in adapter.complete_stream(_make_request())]

        text_tokens = [t for t in tokens if not t.is_final]
        final_tokens = [t for t in tokens if t.is_final]

        assert [t.text for t in text_tokens] == ["Hello! How can I help you today?"]
        assert len(final_tokens) == 1
        assert final_tokens[0].text == ""

    @respx.mock
    async def test_reasoning_tokens_are_not_yielded(self) -> None:
        """Reasoning chunks (delta.content='') must be completely absent from output."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=_sse_fixture())
        )
        adapter = _make_adapter()
        tokens = [t async for t in adapter.complete_stream(_make_request())]

        # No token should carry reasoning text — reasoning is in a different field
        for t in tokens:
            assert "Let me think" not in t.text

    # ---------------------------------------------------------------------------
    # Error classification from HTTP status
    # ---------------------------------------------------------------------------

    @respx.mock
    async def test_quota_exhausted_raises_adapter_quota_exhausted(self) -> None:
        """HTTP 429 with quota keyword raises AdapterQuotaExhausted."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(429, content=_sse_error("daily quota limit exceeded"))
        )
        with pytest.raises(AdapterQuotaExhausted, match="quota"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_quota_exhausted_has_openrouter_shared_cooldown_key(self) -> None:
        """AdapterQuotaExhausted from OpenRouter must have cooldown_key='openrouter_shared'."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(429, content=_sse_error("daily quota limit exceeded"))
        )
        with pytest.raises(AdapterQuotaExhausted) as exc_info:
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

        assert exc_info.value.cooldown_key == "openrouter_shared"

    @respx.mock
    async def test_rate_limit_429_raises_adapter_rate_limit(self) -> None:
        """HTTP 429 without quota keywords raises AdapterRateLimit."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(429, content=_sse_error("too many requests"))
        )
        with pytest.raises(AdapterRateLimit, match="rate limited"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_auth_failure_401_raises_adapter_unavailable(self) -> None:
        """HTTP 401 raises AdapterUnavailable."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(401, content=_sse_error("Invalid API key", code=401))
        )
        with pytest.raises(AdapterUnavailable, match="auth failed"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_auth_failure_403_raises_adapter_unavailable(self) -> None:
        """HTTP 403 raises AdapterUnavailable."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(403, content=_sse_error("Forbidden", code=403))
        )
        with pytest.raises(AdapterUnavailable, match="auth failed"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_server_error_5xx_raises_adapter_rate_limit(self) -> None:
        """HTTP 503 raises AdapterRateLimit (treated as transient, brief cooldown)."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(503, content=_sse_error("Service Unavailable", code=503))
        )
        with pytest.raises(AdapterRateLimit, match="server error"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    # ---------------------------------------------------------------------------
    # Error classification from inline SSE errors
    # ---------------------------------------------------------------------------

    @respx.mock
    async def test_content_filter_finish_reason_raises_adapter_refusal(self) -> None:
        """finish_reason='content_filter' in the stream raises AdapterRefusal."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=_sse_content_filter())
        )
        with pytest.raises(AdapterRefusal, match="content filter"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_inline_quota_error_raises_adapter_quota_exhausted(self) -> None:
        """Inline error chunk with quota message raises AdapterQuotaExhausted."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(
                200, content=_sse_inline_error("daily quota limit exceeded", 429)
            )
        )
        with pytest.raises(AdapterQuotaExhausted):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    # ---------------------------------------------------------------------------
    # Timeout
    # ---------------------------------------------------------------------------

    @respx.mock
    async def test_timeout_raises_adapter_timeout(self) -> None:
        """httpx.TimeoutException raises AdapterTimeout."""
        respx.post(_COMPLETIONS_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(AdapterTimeout, match="timed out"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_connect_error_raises_adapter_unavailable(self) -> None:
        """httpx.ConnectError raises AdapterUnavailable."""
        respx.post(_COMPLETIONS_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(AdapterUnavailable, match="connection failed"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    # ---------------------------------------------------------------------------
    # Fallback model logic
    # ---------------------------------------------------------------------------

    @respx.mock
    async def test_fallback_model_used_when_primary_not_found(self) -> None:
        """HTTP 400 'not a valid model ID' on primary triggers fallback slug."""
        not_found_body = _sse_error("bad/model:free is not a valid model ID", code=400)
        respx.post(_COMPLETIONS_URL).mock(
            side_effect=[
                httpx.Response(400, content=not_found_body),
                httpx.Response(200, content=_sse_fixture()),
            ]
        )
        adapter = _make_adapter(model="bad/model:free", fallback_models=["good/model:free"])
        tokens = [t async for t in adapter.complete_stream(_make_request())]

        text_tokens = [t for t in tokens if not t.is_final]
        assert text_tokens[0].text == "Hello! How can I help you today?"

    @respx.mock
    async def test_all_fallbacks_fail_raises_adapter_unavailable(self) -> None:
        """When every slug returns 400, AdapterUnavailable is raised with all slugs."""
        not_found_body = _sse_error("slug is not a valid model ID", code=400)
        respx.post(_COMPLETIONS_URL).mock(
            side_effect=[
                httpx.Response(400, content=not_found_body),
                httpx.Response(400, content=not_found_body),
                httpx.Response(400, content=not_found_body),
            ]
        )
        adapter = _make_adapter(
            model="bad/a:free",
            fallback_models=["bad/b:free", "bad/c:free"],
        )
        with pytest.raises(AdapterUnavailable, match="All slugs exhausted"):
            async for _ in adapter.complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_non_model_400_is_not_a_fallback_trigger(self) -> None:
        """HTTP 400 without 'not a valid model ID' raises AdapterUnavailable directly."""
        bad_body = _sse_error("messages array is empty", code=400)
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(400, content=bad_body)
        )
        with pytest.raises(AdapterUnavailable, match="bad request"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


class TestComplete:
    @respx.mock
    async def test_content_equals_concatenated_stream_text(self) -> None:
        """complete() joins text tokens into NexusResponse.content."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=_sse_fixture())
        )
        adapter = _make_adapter()
        response = await adapter.complete(_make_request())

        assert response.content == "Hello! How can I help you today?"
        assert response.auth_mode == "api_key_free_tier"
        assert response.finish_reason == "stop"
        assert response.model_id == "test/model:free"


# ---------------------------------------------------------------------------
# count_tokens / available_context
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_hello_world_returns_approximately_3(self) -> None:
        """'hello world' (11 chars / 3.9) ≈ 2; clamped to minimum 1."""
        adapter = _make_adapter()
        result = adapter.count_tokens("hello world")
        # 11 / 3.9 ≈ 2.8 → int → 2
        assert result == max(1, int(11 / 3.9))

    def test_empty_string_returns_1(self) -> None:
        adapter = _make_adapter()
        assert adapter.count_tokens("") == 1

    def test_available_context_returns_configured_window(self) -> None:
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            adapter = OpenRouterAdapter(model="m", context_window=200_000)
        assert adapter.available_context() == 200_000

    def test_estimated_cost_returns_zero_for_free_tier(self) -> None:
        adapter = _make_adapter()
        cost = adapter.estimated_cost(_make_request())
        assert cost["usd"] == 0.0
        assert cost["pro_quota_pct"] == 0.0


# ---------------------------------------------------------------------------
# reasoning_mode (P2c-T04)
# ---------------------------------------------------------------------------


class TestReasoningMode:
    def test_reasoning_mode_stored_on_adapter(self) -> None:
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            adapter = OpenRouterAdapter(model="test/model:free", reasoning_mode="exclude")
        assert adapter.reasoning_mode == "exclude"

    def test_no_reasoning_mode_by_default(self) -> None:
        adapter = _make_adapter()
        assert adapter.reasoning_mode is None

    @respx.mock
    async def test_reasoning_exclude_adds_payload_param(self) -> None:
        """reasoning_mode='exclude' must include {"reasoning": {"exclude": True}} in POST body."""
        import json as _json

        route = respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=_sse_fixture())
        )
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            adapter = OpenRouterAdapter(model="test/model:free", reasoning_mode="exclude")
        [_ async for _ in adapter.complete_stream(_make_request())]

        sent = _json.loads(route.calls.last.request.content)
        assert "reasoning" in sent
        assert sent["reasoning"]["exclude"] is True

    @respx.mock
    async def test_no_reasoning_param_without_mode(self) -> None:
        """Without reasoning_mode, the payload must NOT include a reasoning key."""
        import json as _json

        route = respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=_sse_fixture())
        )
        adapter = _make_adapter()
        [_ async for _ in adapter.complete_stream(_make_request())]

        sent = _json.loads(route.calls.last.request.content)
        assert "reasoning" not in sent


# ---------------------------------------------------------------------------
# Nexus voice injection (Phase 3W)
# ---------------------------------------------------------------------------


class TestNexusVoice:
    def test_payload_leads_with_nexus_style_system_message(self) -> None:
        from orchestrator.prompts import NEXUS_STYLE

        adapter = _make_adapter()
        payload = adapter._build_payload("test/model:free", _make_request("hi"))
        assert payload["messages"][0] == {"role": "system", "content": NEXUS_STYLE}
        assert payload["messages"][1] == {"role": "user", "content": "hi"}

    @respx.mock
    async def test_voice_reaches_the_wire(self) -> None:
        """The NEXUS_STYLE system message must appear in the actual POST body."""
        import json as _json

        from orchestrator.prompts import NEXUS_STYLE

        route = respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=_sse_fixture())
        )
        adapter = _make_adapter()
        [_ async for _ in adapter.complete_stream(_make_request())]

        sent = _json.loads(route.calls.last.request.content)
        assert sent["messages"][0]["role"] == "system"
        assert sent["messages"][0]["content"] == NEXUS_STYLE


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @respx.mock
    async def test_healthy_when_model_in_catalogue(self) -> None:
        """health_check returns healthy=True when GET /models succeeds and model is listed."""
        respx.get(_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "test/model:free"}]})
        )
        status = await _make_adapter().health_check()
        assert status.healthy is True
        assert status.detail == "catalogue-listed"

    @respx.mock
    async def test_unhealthy_when_models_endpoint_returns_non_200(self) -> None:
        """health_check returns healthy=False on non-200 from /models."""
        respx.get(_MODELS_URL).mock(return_value=httpx.Response(401))
        status = await _make_adapter().health_check()
        assert status.healthy is False
        assert "401" in status.detail

    @respx.mock
    async def test_unhealthy_when_connection_error(self) -> None:
        """health_check returns healthy=False on network error."""
        respx.get(_MODELS_URL).mock(side_effect=httpx.ConnectError("refused"))
        status = await _make_adapter().health_check()
        assert status.healthy is False
        assert status.detail != ""

    @respx.mock
    async def test_unhealthy_when_timeout(self) -> None:
        """health_check returns healthy=False on timeout."""
        respx.get(_MODELS_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        status = await _make_adapter().health_check()
        assert status.healthy is False

    @respx.mock
    async def test_unhealthy_when_model_absent_from_catalogue(self) -> None:
        """Shallow check: model not listed in /models catalogue → unhealthy."""
        respx.get(_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "other/model:free"}]})
        )
        status = await _make_adapter().health_check()
        assert status.healthy is False
        assert "not in catalogue" in status.detail

    @respx.mock
    async def test_deep_probe_healthy_on_success(self) -> None:
        """Deep probe: valid completion body → healthy with detail='probe-ok'."""
        respx.get(_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "test/model:free"}]})
        )
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]},
            )
        )
        status = await _make_adapter().health_check(deep=True)
        assert status.healthy is True
        assert status.detail == "probe-ok"

    @respx.mock
    async def test_deep_probe_unhealthy_on_error_body(self) -> None:
        """Deep probe: 200 with error body → unhealthy; reason includes classified detail."""
        respx.get(_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "test/model:free"}]})
        )
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"error": {"code": 404, "message": "Provider returned error"}},
            )
        )
        status = await _make_adapter().health_check(deep=True)
        assert status.healthy is False
        assert "unavailable" in status.detail.lower() or "provider" in status.detail.lower()


# ---------------------------------------------------------------------------
# Error classification — new failure modes (P2c-T01)
# ---------------------------------------------------------------------------


class TestErrorClassificationP2c:
    @respx.mock
    async def test_http_404_raises_adapter_model_unavailable(self) -> None:
        """HTTP 404 → AdapterModelUnavailable (not rate-limit or AdapterUnavailable)."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(
                404, content=_sse_error("Provider returned error", code=404)
            )
        )
        with pytest.raises(AdapterModelUnavailable):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_200_with_error_body_provider_returned_error(self) -> None:
        """KEY REGRESSION (2026-05-29): HTTP 200 + error body → AdapterModelUnavailable.

        OpenRouter returns HTTP 200 with {"error":{"code":404,...}} for dead providers.
        Previously this was silently ignored (or misclassified as rate-limit). Must now
        raise AdapterModelUnavailable.
        """
        import json as _json

        body = _json.dumps(
            {"error": {"code": 404, "message": "Provider returned error"}}
        ).encode()
        respx.post(_COMPLETIONS_URL).mock(return_value=httpx.Response(200, content=body))
        with pytest.raises(AdapterModelUnavailable, match="Provider returned error"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_200_with_sse_error_provider_returned_error(self) -> None:
        """HTTP 200 + SSE-wrapped error body → AdapterModelUnavailable."""
        body = _sse_inline_error("Provider returned error", 404)
        respx.post(_COMPLETIONS_URL).mock(return_value=httpx.Response(200, content=body))
        with pytest.raises(AdapterModelUnavailable, match="Provider returned error"):
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass

    @respx.mock
    async def test_200_with_metadata_raw_nested_model_not_found(self) -> None:
        """metadata.raw with inner 'Model not found' → AdapterModelUnavailable, includes reason."""
        import json as _json

        body = _json.dumps(
            {
                "error": {
                    "code": 200,
                    "message": "Provider returned error",
                    "metadata": {
                        "raw": _json.dumps({"error": {"message": "Model not found"}})
                    },
                }
            }
        ).encode()
        respx.post(_COMPLETIONS_URL).mock(return_value=httpx.Response(200, content=body))
        with pytest.raises(AdapterModelUnavailable) as exc_info:
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass
        assert "Model not found" in str(exc_info.value)

    @respx.mock
    async def test_429_with_daily_limit_raises_quota_exhausted_with_cooldown_key(self) -> None:
        """429 + 'daily limit reached' → AdapterQuotaExhausted, cooldown_key='openrouter_shared'."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(429, content=_sse_error("daily limit reached"))
        )
        with pytest.raises(AdapterQuotaExhausted) as exc_info:
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass
        assert exc_info.value.cooldown_key == "openrouter_shared"

    @respx.mock
    async def test_429_plain_raises_rate_limit_with_cooldown_45(self) -> None:
        """HTTP 429 without quota keywords → AdapterRateLimit with cooldown_seconds=45."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(429, content=_sse_error("rate limit exceeded"))
        )
        with pytest.raises(AdapterRateLimit) as exc_info:
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass
        assert exc_info.value.cooldown_seconds == 45

    @respx.mock
    async def test_503_raises_rate_limit_with_cooldown_30(self) -> None:
        """HTTP 503 → AdapterRateLimit with cooldown_seconds=30 (transient server error)."""
        respx.post(_COMPLETIONS_URL).mock(
            return_value=httpx.Response(503, content=_sse_error("Service Unavailable", code=503))
        )
        with pytest.raises(AdapterRateLimit) as exc_info:
            async for _ in _make_adapter().complete_stream(_make_request()):
                pass
        assert exc_info.value.cooldown_seconds == 30


# ---------------------------------------------------------------------------
# _extract_error unit tests
# ---------------------------------------------------------------------------


class TestExtractError:
    def test_str_body_returns_str_and_no_code(self) -> None:
        msg, code = _extract_error("plain error text")
        assert msg == "plain error text"
        assert code is None

    def test_bytes_body_decoded_as_plain_text(self) -> None:
        msg, code = _extract_error(b"service unavailable")
        assert msg == "service unavailable"
        assert code is None

    def test_dict_body_extracts_message_and_code(self) -> None:
        body = {"error": {"message": "Provider returned error", "code": 404}}
        msg, code = _extract_error(body)
        assert msg == "Provider returned error"
        assert code == 404

    def test_dict_body_with_metadata_raw_includes_inner_reason(self) -> None:
        import json as _json

        body = {
            "error": {
                "message": "Provider returned error",
                "code": 404,
                "metadata": {"raw": _json.dumps({"error": {"message": "Model not found"}})},
            }
        }
        msg, code = _extract_error(body)
        assert "Provider returned error" in msg
        assert "Model not found" in msg
        assert code == 404

    def test_bytes_body_with_json_extracts_message(self) -> None:
        import json as _json

        body = _json.dumps({"error": {"message": "rate limit", "code": 429}}).encode()
        msg, code = _extract_error(body)
        assert msg == "rate limit"
        assert code == 429
