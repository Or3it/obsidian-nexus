"""Google AI Studio adapter: Gemini via the google-genai SDK.

Verified 2026-05-16, google-genai v2.3.0:
  - Package: google-genai (NOT the legacy google-generativeai; incompatible APIs)
  - Async streaming: async for chunk in await client.aio.models.generate_content_stream(...)
  - Chunk text: chunk.candidates[0].content.parts[i].text
  - Final chunk: finish_reason == FinishReason.STOP, text=""
  - Safety block: finish_reason == FinishReason.SAFETY in response (not raised as exception)
  - Quota 429: ClientError status=RESOURCE_EXHAUSTED, PerDay quota ID in body
  - Rate limit 429: ClientError status=RESOURCE_EXHAUSTED, PerMinute quota ID in body
  - Async token counting: await client.aio.models.count_tokens() — free endpoint
  - Default model gemini-3.1-pro-preview has free-tier quota=0 on this account.
    See OPEN_QUESTIONS.md [2026-05-16] for details and fallback recommendation.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import google.genai as genai
import google.genai.errors as ge_errors
from google.genai import types as genai_types

from ..types import HealthStatus, NexusRequest, NexusResponse, Token
from .base import (
    AdapterContextOverflow,
    AdapterQuotaExhausted,
    AdapterRateLimit,
    AdapterRefusal,
    AdapterTimeout,
    AdapterUnavailable,
)

log = logging.getLogger(__name__)

_CHARS_PER_TOKEN_FALLBACK = 3.5  # Gemini approximation when SDK counter unavailable
_HEALTH_CACHE_TTL = timedelta(minutes=5)


class GoogleAdapter:
    name = "google"

    def __init__(
        self,
        model: str = "gemini-3.1-pro-preview",
        api_key_env: str = "GOOGLE_AI_STUDIO_API_KEY",
        context_window: int = 1_048_576,
        max_output_tokens: int = 65_536,
        timeout_seconds: int = 120,
        thinking_level: str = "medium",  # reserved; not yet wired to SDK config
    ) -> None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(
                f"{api_key_env} environment variable not set. "
                "Generate a key at https://aistudio.google.com/apikey and export it."
            )
        self.model = model
        self._client = genai.Client(api_key=api_key)
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout_seconds
        self.thinking_level = thinking_level
        self._health_cache: HealthStatus | None = None
        self._health_cache_until: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        """Stream tokens from Gemini. Raises AdapterError subclasses on failure."""
        contents, system_instruction = self._build_contents(request)

        # Pre-flight context overflow check via the free count_tokens endpoint.
        try:
            count_result = await self._client.aio.models.count_tokens(
                model=self.model,
                contents=contents,
            )
            prompt_tokens: int = count_result.total_tokens or 0
        except Exception:
            log.debug("count_tokens pre-flight failed; falling back to char-ratio estimate")
            prompt_text = " ".join(
                p.text or "" for c in contents for p in (c.parts or [])
            )
            if system_instruction:
                prompt_text = system_instruction + " " + prompt_text
            prompt_tokens = max(1, int(len(prompt_text) / _CHARS_PER_TOKEN_FALLBACK))

        if prompt_tokens > int(self.context_window * 0.9):
            raise AdapterContextOverflow(
                f"Prompt {prompt_tokens} tokens exceeds 90% of "
                f"{self.context_window}-token context window"
            )

        config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=request.max_output_tokens or self.max_output_tokens,
        )

        try:
            stream = await asyncio.wait_for(
                self._client.aio.models.generate_content_stream(
                    model=self.model,
                    contents=contents,
                    config=config,
                ),
                timeout=float(self.timeout),
            )
        except TimeoutError:
            raise AdapterTimeout(f"Gemini timed out after {self.timeout}s")
        except ge_errors.ClientError as exc:
            raise self._classify_client_error(exc) from exc
        except ge_errors.ServerError as exc:
            raise AdapterUnavailable(f"Gemini server error: {exc}") from exc

        try:
            async for chunk in stream:
                candidates = chunk.candidates
                if not candidates:
                    continue
                candidate = candidates[0]
                finish_reason = candidate.finish_reason

                if finish_reason == genai_types.FinishReason.SAFETY:
                    raise AdapterRefusal("Gemini refused the request due to safety policy")

                if finish_reason == genai_types.FinishReason.STOP:
                    yield Token(text="", is_final=True)
                    return

                content = candidate.content
                if content is None:
                    continue
                for part in content.parts or []:
                    if part.text:
                        yield Token(text=part.text, is_final=False)

        except (
            AdapterRefusal,
            AdapterContextOverflow,
            AdapterUnavailable,
            AdapterQuotaExhausted,
            AdapterRateLimit,
            AdapterTimeout,
        ):
            raise
        except ge_errors.ClientError as exc:
            raise self._classify_client_error(exc) from exc
        except ge_errors.ServerError as exc:
            raise AdapterUnavailable(f"Gemini server error: {exc}") from exc

    def count_tokens(self, text: str) -> int:
        """Count tokens via sync SDK endpoint; fall back to char-ratio on failure."""
        try:
            result = self._client.models.count_tokens(
                model=self.model,
                contents=text,
            )
            total = result.total_tokens
            return total if total else max(1, int(len(text) / _CHARS_PER_TOKEN_FALLBACK))
        except Exception:
            log.debug("count_tokens SDK call failed; using char-ratio fallback")
            return max(1, int(len(text) / _CHARS_PER_TOKEN_FALLBACK))

    def available_context(self) -> int:
        return self.context_window

    def estimated_cost(self, request: NexusRequest) -> dict:  # type: ignore[type-arg]
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self) -> HealthStatus:
        """Probe with a 1-token request; cache result for 5 minutes."""
        now = datetime.now(UTC)
        if (
            self._health_cache is not None
            and self._health_cache_until is not None
            and now < self._health_cache_until
        ):
            return self._health_cache

        try:
            await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self.model,
                    contents="hi",
                    config=genai_types.GenerateContentConfig(max_output_tokens=1),
                ),
                timeout=10.0,
            )
            status = HealthStatus(healthy=True, last_checked=now)
        except Exception as exc:
            status = HealthStatus(healthy=False, last_checked=now, detail=str(exc)[:200])

        self._health_cache = status
        self._health_cache_until = now + _HEALTH_CACHE_TTL
        return status

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_contents(
        self, request: NexusRequest
    ) -> tuple[list[genai_types.Content], str | None]:
        """Convert NexusRequest messages → (contents_list, system_instruction)."""
        system_parts: list[str] = []
        contents: list[genai_types.Content] = []

        for msg in request.messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            elif msg.role == "user":
                contents.append(
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(text=msg.content)],
                    )
                )
            else:
                contents.append(
                    genai_types.Content(
                        role="model",
                        parts=[genai_types.Part(text=msg.content)],
                    )
                )

        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return contents, system_instruction

    @staticmethod
    def _classify_client_error(
        exc: ge_errors.ClientError,
    ) -> AdapterQuotaExhausted | AdapterRateLimit | AdapterUnavailable:
        """Map a 4xx ClientError to the most specific AdapterError subclass.

        Both daily-quota exhaustion and per-minute rate limits arrive as HTTP 429
        RESOURCE_EXHAUSTED.  The error body contains quota IDs that distinguish them:
          - PerDay → daily quota gone → AdapterQuotaExhausted (cooldown until tomorrow)
          - PerMinute only → transient rate limit → AdapterRateLimit (retry soon)
        """
        msg = str(exc)
        if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
            if "PerDay" in msg or "daily" in msg.lower():
                return AdapterQuotaExhausted(f"Gemini daily quota exhausted: {msg[:300]}")
            return AdapterRateLimit(f"Gemini rate limited: {msg[:300]}")
        return AdapterUnavailable(f"Gemini client error: {msg[:300]}")
