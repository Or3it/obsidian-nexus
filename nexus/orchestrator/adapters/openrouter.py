"""OpenRouter adapter: Rungs 2, 3, and 4 via OpenRouter's OpenAI-compatible API.

Verified 2026-05-16:
  - API: POST https://openrouter.ai/api/v1/chat/completions (OpenAI-compatible)
  - SSE format: data: {json}\\n\\n per chunk, data: [DONE] at end
  - Keepalive: ': OPENROUTER PROCESSING' — skip lines starting with ':'
  - Reasoning chunks: delta.content="" + delta.reasoning="..." — skip, not user-visible
  - Content chunks: delta.content="text" (non-empty) — yield as Token
  - Stop chunk: finish_reason="stop" — yield final Token
  - Model-not-found: HTTP 400 with "not a valid model ID" in error body
  - Quota exhausted: HTTP 429 with "quota"/"daily"/"limit" in error body
  - Rate limited: HTTP 429 without quota keywords
  - All three OpenRouter rungs share one daily pool -> cooldown_key="openrouter_shared"
  - Verified slugs: minimax/minimax-m2.5:free, z-ai/glm-4.5-air:free, qwen/qwen3-coder:free
    See OPEN_QUESTIONS.md [2026-05-16] for substitution details.
"""

import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ..prompts import NEXUS_STYLE
from ..types import HealthStatus, NexusRequest, NexusResponse, Token
from .base import (
    AdapterModelUnavailable,
    AdapterQuotaExhausted,
    AdapterRateLimit,
    AdapterRefusal,
    AdapterTimeout,
    AdapterUnavailable,
)

log = logging.getLogger(__name__)

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
_COOLDOWN_KEY = "openrouter_shared"


class _ModelNotAvailable(Exception):
    """Internal: this specific slug is unavailable; cascade to the next fallback."""


class OpenRouterAdapter:
    """One adapter class, instantiated per rung with different model slugs.

    Rungs 2, 3, and 4 all share one OpenRouter API key and daily quota pool.
    When quota is exhausted, AdapterQuotaExhausted is raised with
    cooldown_key="openrouter_shared" so the cascade engine cools all three
    rungs together (P2a-T08 handles cross-rung cooldown logic).
    """

    name = "openrouter"

    def __init__(
        self,
        model: str,
        api_key_env: str = "OPENROUTER_API_KEY",
        context_window: int = 128_000,
        max_output_tokens: int = 4096,
        timeout_seconds: int = 180,
        chars_per_token: float = 3.9,
        fallback_models: list[str] | None = None,
        http_referer: str = "https://github.com/Or3it/obsidian-nexus",
        x_title: str = "Obsidian Nexus",
        reasoning_mode: str | None = None,
    ) -> None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(
                f"{api_key_env} not set. Sign up at https://openrouter.ai/keys and export the key."
            )
        self.model = model
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout_seconds
        self.chars_per_token = chars_per_token
        self.fallback_models = fallback_models or []
        self.reasoning_mode = reasoning_mode
        self._base_url = OPENROUTER_API_BASE
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": http_referer,
            "X-Title": x_title,
        }

    async def complete(self, request: NexusRequest) -> NexusResponse:
        """Single-shot completion: collect the full stream internally."""
        chunks: list[str] = []
        started = datetime.now(UTC)
        async for token in self.complete_stream(request):
            if not token.is_final:
                chunks.append(token.text)
        content = "".join(chunks)
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        return NexusResponse(
            content=content,
            model_id=self.model,
            auth_mode="api_key_free_tier",
            estimated_tokens=self.count_tokens(content),
            duration_ms=duration_ms,
            finish_reason="stop",
        )

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """Stream tokens; tries primary model slug then fallback_models in order."""
        models_to_try = [self.model] + self.fallback_models
        last_exc: Exception | None = None
        for candidate in models_to_try:
            try:
                async for token in self._stream_from_model(candidate, request):
                    yield token
                return
            except _ModelNotAvailable as exc:
                log.debug("Model %s unavailable, trying next fallback", candidate)
                last_exc = exc
                continue
        raise AdapterUnavailable(
            f"All slugs exhausted — tried: {models_to_try}. Last error: {last_exc}"
        )

    def _build_payload(self, model_slug: str, request: NexusRequest) -> dict[str, Any]:
        """Assemble the chat-completions payload. NEXUS_STYLE leads every call."""
        messages = [{"role": "system", "content": NEXUS_STYLE}] + [
            {"role": m.role, "content": m.content} for m in request.messages
        ]
        payload: dict[str, Any] = {
            "model": model_slug,
            "messages": messages,
            "max_tokens": request.max_output_tokens or self.max_output_tokens,
            "stream": True,
        }
        if self.reasoning_mode == "exclude":
            # Suppress thinking tokens — OpenRouter extension; prevents reasoning text
            # from bleeding into the content field on models like Nemotron.
            payload["reasoning"] = {"exclude": True}
        return payload

    async def _stream_from_model(
        self, model_slug: str, request: NexusRequest
    ) -> AsyncIterator[Token]:
        """Stream from one specific model slug. Raises _ModelNotAvailable for bad slugs."""
        payload = self._build_payload(model_slug, request)
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout)) as client:
                async with client.stream(
                    "POST",
                    f"{OPENROUTER_API_BASE}/chat/completions",
                    json=payload,
                    headers=self._headers,
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        raise _classify_error(response.status_code, body, model_slug)
                    async for line in response.aiter_lines():
                        token = _parse_sse_line(line, model_slug)
                        if token is not None:
                            yield token
                            if token.is_final:
                                return
        except (
            _ModelNotAvailable,
            AdapterModelUnavailable,
            AdapterUnavailable,
            AdapterQuotaExhausted,
            AdapterRateLimit,
            AdapterRefusal,
            AdapterTimeout,
        ):
            raise
        except httpx.TimeoutException as exc:
            raise AdapterTimeout(f"OpenRouter timed out after {self.timeout}s") from exc
        except httpx.ConnectError as exc:
            raise AdapterUnavailable(f"OpenRouter connection failed: {exc}") from exc

    def count_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def available_context(self) -> int:
        return self.context_window

    def estimated_cost(self, request: NexusRequest) -> dict[str, Any]:
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self, *, deep: bool = False) -> HealthStatus:
        """Verify adapter health.

        Shallow (default): GET /models, confirm API reachable and the configured model
        id appears in the catalogue. Cheap — no completion quota used.
        Deep (deep=True): also send a 1-token probe to confirm the provider actually
        serves it right now. Costs one request — only use on the recovery edge (T05).
        """
        now = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{self._base_url}/models",
                    headers=self._headers,
                )
            if r.status_code != 200:
                return HealthStatus(
                    healthy=False, last_checked=now, detail=f"models endpoint {r.status_code}"
                )
            ids = {m["id"] for m in r.json().get("data", [])}
            if self.model not in ids:
                return HealthStatus(
                    healthy=False, last_checked=now, detail=f"{self.model} not in catalogue"
                )
        except Exception as exc:
            return HealthStatus(healthy=False, last_checked=now, detail=f"unreachable: {exc}")

        if not deep:
            return HealthStatus(healthy=True, last_checked=now, detail="catalogue-listed")

        # Deep probe — 1 token, confirms the provider serves it right now
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                )
            body = r.json()
            if "error" in body:
                err = _classify_error(r.status_code, body)
                return HealthStatus(healthy=False, last_checked=now, detail=str(err))
            return HealthStatus(healthy=True, last_checked=now, detail="probe-ok")
        except Exception as exc:
            return HealthStatus(healthy=False, last_checked=now, detail=f"probe failed: {exc}")


def _parse_sse_line(line: str, model_slug: str) -> Token | None:
    """Parse one SSE line; return Token, None (skip), or raise an AdapterError."""
    if not line or line.startswith(":"):
        return None

    # Bare JSON body — OpenRouter sometimes returns {"error":...} on HTTP 200 without
    # SSE wrapping (observed 2026-05-29 with dead providers).
    if line.startswith("{"):
        try:
            bare: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            return None
        if "error" in bare:
            raise _classify_error(200, bare, model_slug)
        return None

    if not line.startswith("data: "):
        return None

    payload = line[6:]
    if payload == "[DONE]":
        return Token(text="", is_final=True)

    try:
        data: dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError:
        log.debug("Non-JSON SSE payload from %s: %.80s", model_slug, payload)
        return None

    if "error" in data:
        raise _classify_error(200, data, model_slug)

    choices: list[Any] = data.get("choices", [])
    if not choices:
        return None

    choice: dict[str, Any] = choices[0]
    finish_reason: str | None = choice.get("finish_reason")

    if finish_reason == "content_filter":
        raise AdapterRefusal("OpenRouter content filter blocked the request")

    if finish_reason == "stop":
        return Token(text="", is_final=True)

    delta: dict[str, Any] = choice.get("delta", {})
    content: str | None = delta.get("content")

    if content:  # non-empty string — actual visible content, not a reasoning chunk
        return Token(text=content, is_final=False)

    return None  # reasoning chunk (content="" + reasoning="...") or empty delta


def _extract_error(body: "dict[str, Any] | str | bytes") -> "tuple[str, int | None]":
    """Pull a human message and any embedded numeric code from an OpenRouter error body.

    Handles:
    - str body (plain text error)
    - bytes body (decoded to str or JSON)
    - dict body like {"error": {"message": ..., "code": ..., "metadata": {"raw": "..."}}}
    The metadata.raw field sometimes carries a nested JSON string with the real provider reason.
    """
    if isinstance(body, bytes):
        raw_bytes: bytes = body
        try:
            body = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw_bytes.decode(errors="replace")[:300], None
    if isinstance(body, str):
        return body[:300], None
    err = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(err, dict):
        return str(err)[:300], None
    msg: str = str(err.get("message", "") or "")
    code = err.get("code")
    meta = err.get("metadata", {})
    raw = meta.get("raw") if isinstance(meta, dict) else None
    if raw:
        try:
            inner = json.loads(raw)
            inner_msg = (
                inner.get("error", {}).get("message")
                if isinstance(inner, dict)
                else None
            )
            if inner_msg:
                msg = f"{msg} ({inner_msg})" if msg else str(inner_msg)
        except Exception:
            msg = f"{msg} {raw}".strip()
    return msg, (int(code) if isinstance(code, (int, float)) else None)


def _classify_error(
    status_code: int,
    body: "dict[str, Any] | str | bytes",
    model_slug: str = "",
) -> Exception:
    """Classify an OpenRouter failure into the most specific adapter exception.

    Inspects BOTH the HTTP status AND the response body — OpenRouter returns
    HTTP 200 with an error object for some provider failures (observed 2026-05-29).
    """
    msg, embedded_code = _extract_error(body)
    lowered = msg.lower()
    effective_status = embedded_code if embedded_code is not None else status_code

    # Model/provider unavailable — 404 anywhere, or provider-error text
    if (
        effective_status == 404
        or "model not found" in lowered
        or "provider returned error" in lowered
        or "no allowed providers" in lowered
    ):
        return AdapterModelUnavailable(
            f"OpenRouter model unavailable: {msg}", cooldown_seconds=300
        )

    # Invalid slug — 400 "not a valid model ID" (triggers fallback in complete_stream)
    if effective_status == 400 and "not a valid model" in lowered:
        slug_prefix = f"{model_slug}: " if model_slug else ""
        return _ModelNotAvailable(f"{slug_prefix}{msg}")

    # Other bad-request errors
    if effective_status == 400:
        return AdapterUnavailable(f"OpenRouter bad request: {msg}")

    # Auth failures
    if effective_status in (401, 403):
        return AdapterUnavailable(f"OpenRouter auth failed ({effective_status}): {msg}")

    # Quota exhausted (account-wide, shared key cooldown).
    # "limit" alone is intentionally excluded — "rate limit exceeded" contains "limit"
    # but is a per-minute throttle, not daily quota exhaustion.
    if effective_status == 429 and ("quota" in lowered or "daily" in lowered):
        return AdapterQuotaExhausted(
            f"OpenRouter daily quota exhausted: {msg}", cooldown_key=_COOLDOWN_KEY
        )

    # Per-minute rate limit
    if effective_status == 429:
        return AdapterRateLimit(f"OpenRouter rate limited: {msg}", cooldown_seconds=45)

    # Transient server error — treat as brief cooldown, not permanent failure
    if 500 <= effective_status < 600:
        return AdapterRateLimit(
            f"OpenRouter server error {effective_status}: {msg}", cooldown_seconds=30
        )

    return AdapterUnavailable(f"OpenRouter error {effective_status}: {msg}")
