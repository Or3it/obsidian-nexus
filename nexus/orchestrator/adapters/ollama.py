"""Ollama adapter: Rung 5 — local sovereignty floor via Ollama HTTP API.

Verified 2026-05-16, Ollama at http://localhost:11434:
  - Streaming endpoint: POST /api/chat with stream=true
  - Format: newline-delimited JSON (NDJSON), one object per line — NOT SSE
  - Content token: {"model":"...","message":{"role":"assistant","content":"text"},"done":false}
  - Final token: {"model":"...","message":{"role":"assistant","content":""},"done":true,
                  "done_reason":"stop","total_duration":...,"eval_count":...}
  - Error chunk: {"error":"description of the error"}
  - Context overflow error text contains "context" or "too long"
  - Model not pulled: HTTP 404
  - Ollama not running: httpx.ConnectError on all requests
  - Health check: GET /api/tags returns {"models":[{"name":"gemma4:latest",...},...]}
  - Cold-start tax: ~25s to load gemma4:latest (9.6 GB) — keep_alive="-1" keeps it resident
  - Confirmed models: gemma4:latest (8B Q4_K_M), qwen2.5-coder:7b (7.6B Q4_K_M)
    See OPEN_QUESTIONS.md [2026-05-16] for details.
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ..types import HealthStatus, NexusRequest, NexusResponse, Token
from .base import (
    AdapterContextOverflow,
    AdapterTimeout,
    AdapterUnavailable,
)

log = logging.getLogger(__name__)


class OllamaAdapter:
    """Rung 5 local adapter. No API key. No rate limits. No network."""

    name = "ollama"

    def __init__(
        self,
        model: str = "gemma4:latest",
        base_url: str = "http://localhost:11434",
        context_window: int = 32_000,
        max_output_tokens: int = 4096,
        timeout_seconds: int = 300,
        keep_alive: int | str = -1,
        chars_per_token: float = 3.9,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout_seconds
        self.keep_alive = keep_alive
        self.chars_per_token = chars_per_token

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
            model_id=f"ollama:{self.model}",
            auth_mode="local",
            estimated_tokens=self.count_tokens(content),
            duration_ms=duration_ms,
            finish_reason="stop",
        )

    async def complete_once(self, prompt: str, max_tokens: int = 10) -> str:
        """Convenience wrapper for single-turn classification prompts.

        Used by the task classifier when backend=ollama. Returns the raw text
        content of the response (caller parses the one-word class from it).
        """
        from ..types import NexusMessage, StyleProfile  # local to avoid circular at module level

        req = NexusRequest(
            thread_id="classifier",
            messages=[NexusMessage(role="user", content=prompt)],
            style_profile=StyleProfile(),
            max_output_tokens=max_tokens,
        )
        response = await self.complete(req)
        return response.content

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """Stream tokens from Ollama via NDJSON. Raises AdapterError subclasses on failure."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "num_predict": request.max_output_tokens or self.max_output_tokens,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout)) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/chat",
                    json=payload,
                ) as response:
                    if response.status_code == 404:
                        raise AdapterUnavailable(
                            f"Ollama model '{self.model}' not found. "
                            f"Run `ollama pull {self.model}` to install it."
                        )
                    if response.status_code >= 500:
                        raise AdapterUnavailable(
                            f"Ollama returned {response.status_code}"
                        )
                    if response.status_code != 200:
                        body = await response.aread()
                        raise AdapterUnavailable(
                            f"Ollama returned {response.status_code}: "
                            f"{body.decode(errors='replace')[:200]}"
                        )
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk: dict[str, Any] = json.loads(line)
                        except json.JSONDecodeError:
                            log.debug("Non-JSON line from Ollama: %.80s", line)
                            continue
                        if "error" in chunk:
                            error_text = str(chunk["error"])
                            low = error_text.lower()
                            if "context" in low or "too long" in low:
                                raise AdapterContextOverflow(error_text)
                            raise AdapterUnavailable(error_text)
                        content_text: str = chunk.get("message", {}).get("content", "") or ""
                        if content_text:
                            yield Token(text=content_text, is_final=False)
                        if chunk.get("done"):
                            yield Token(text="", is_final=True)
                            return
        except (
            AdapterUnavailable,
            AdapterContextOverflow,
            AdapterTimeout,
        ):
            raise
        except httpx.ConnectError as exc:
            raise AdapterUnavailable(
                f"Cannot reach Ollama at {self.base_url}. Is `ollama serve` running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise AdapterTimeout(
                f"Ollama timed out after {self.timeout}s. "
                "Local model may still be loading (cold-start can take 30s+ for large models)."
            ) from exc

    def count_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def available_context(self) -> int:
        return self.context_window

    def estimated_cost(self, request: NexusRequest) -> dict[str, Any]:
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self) -> HealthStatus:
        """Check Ollama is running and the configured model is pulled."""
        now = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
            if response.status_code != 200:
                return HealthStatus(
                    healthy=False,
                    last_checked=now,
                    detail=f"GET /api/tags returned {response.status_code}",
                )
            tags_data: dict[str, Any] = response.json()
            models: list[Any] = tags_data.get("models", [])
            model_names: list[str] = [str(m.get("name", "")) for m in models]
            base_name = self.model.split(":")[0]
            if not any(
                self.model == n or n.startswith(f"{base_name}:")
                for n in model_names
            ):
                return HealthStatus(
                    healthy=False,
                    last_checked=now,
                    detail=(
                        f"Model '{self.model}' not pulled. "
                        f"Available: {model_names}. "
                        f"Run `ollama pull {self.model}` to install it."
                    ),
                )
            return HealthStatus(healthy=True, last_checked=now)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return HealthStatus(
                healthy=False,
                last_checked=now,
                detail=f"Ollama unreachable at {self.base_url}: {exc}",
            )
