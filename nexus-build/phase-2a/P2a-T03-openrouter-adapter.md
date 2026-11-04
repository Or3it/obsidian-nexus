# P2a-T03 — OpenRouter Adapter (Rungs 3 & 4)

## Goal

One adapter, two rungs. OpenRouter's free tier hosts the models for Rungs 3 (GLM-5.1) and 4 (Qwen3-Coder-480B / DeepSeek R1). Same API key, same HTTP client, different model slugs.

## Read first

- `shared/adapter-protocol.md`
- `shared/cascade-contract.md`
- `shared/trigger-taxonomy.md` — note `OPENROUTER_QUOTA` is special: when it fires, both Rung 3 and Rung 4 must be marked exhausted together (they share the daily pool).

## Verification step — HARD GATE

1. Confirm current free model slugs at https://openrouter.ai/models?max_price=0. The PRD writes:
   - `zhipu/glm-5.1:free` for Rung 3
   - `qwen/qwen3-coder-480b:free` for Rung 4 (coding)
   - `deepseek/deepseek-r1:free` for Rung 4 (reasoning)

   These slugs *will* drift. Capture currently-available equivalents. If GLM-5.1 isn't available, pick the strongest free open-weight reasoning model. If Qwen3-Coder isn't available, pick the strongest free coding model.

2. Confirm OpenRouter free-tier rate limits at https://openrouter.ai/docs/limits. The PRD claims ~20 RPM, ~200 RPD shared across all free models on a single key.

3. Confirm the chat-completions API is OpenAI-compatible (it should be). Send a one-token test with `curl`:

   ```bash
   curl https://openrouter.ai/api/v1/chat/completions \
     -H "Authorization: Bearer $OPENROUTER_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"<verified-slug>","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
   ```

4. Confirm streaming format. OpenRouter streams as SSE with `data: {json}\n\n` per chunk and `data: [DONE]` at end. Capture sample to `tests/fixtures/openrouter_stream.txt`.

Append findings to `OPEN_QUESTIONS.md`.

## Files to create or replace

```
nexus/orchestrator/adapters/openrouter.py
nexus/tests/test_openrouter_adapter.py
nexus/tests/fixtures/openrouter_stream.txt
```

`httpx` is already a Phase 1 dependency — no new packages needed.

## `nexus/orchestrator/adapters/openrouter.py`

```python
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from ..types import NexusRequest, NexusResponse, Token, HealthStatus
from .base import (
    AdapterUnavailable,
    AdapterQuotaExhausted,
    AdapterTimeout,
    AdapterRefusal,
    AdapterRateLimit,
    AdapterContextOverflow,
)

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


class OpenRouterAdapter:
    """
    Single adapter class; instantiated twice (Rung 3 and Rung 4) with different model slugs.
    """
    name = "openrouter"

    def __init__(
        self,
        model: str,                              # e.g. "zhipu/glm-5.1:free"
        api_key_env: str = "OPENROUTER_API_KEY",
        context_window: int = 128_000,           # verify per-model
        max_output_tokens: int = 4096,
        timeout_seconds: int = 180,              # free models can be slow
        chars_per_token: float = 3.9,            # OpenAI-compat tokenizer estimate
        fallback_models: list[str] | None = None,  # try these if the primary slug is removed
        http_referer: str = "https://github.com/Or3it/obsidian-nexus",
        x_title: str = "Obsidian Nexus",
    ):
        self.model = model
        self.api_key = os.environ.get(api_key_env)
        if not self.api_key:
            raise ValueError(
                f"{api_key_env} not set. Sign up at https://openrouter.ai/keys and export the key."
            )
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout_seconds
        self.chars_per_token = chars_per_token
        self.fallback_models = fallback_models or []
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": http_referer,
            "X-Title": x_title,
        }

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """
        POST /chat/completions with stream=true. SSE response.

        Trigger mapping:
          - HTTP 429 with body containing "quota" or "daily limit" → AdapterQuotaExhausted
            (cascade will mark BOTH Rungs 3 and 4 exhausted on this)
          - HTTP 429 otherwise → AdapterRateLimit
          - HTTP 4xx auth → AdapterUnavailable
          - HTTP 5xx after retries → AdapterUnavailable
          - Model slug not found (404 or specific error body) → try next fallback_models entry
          - Timeout → AdapterTimeout
          - Refusal token in stream → AdapterRefusal
        """
        models_to_try = [self.model] + self.fallback_models
        last_exc: Exception | None = None
        for candidate_model in models_to_try:
            try:
                async for token in self._stream_from_model(candidate_model, request):
                    yield token
                return
            except _ModelNotAvailable as exc:
                last_exc = exc
                continue
        if last_exc:
            raise AdapterUnavailable(
                f"No model slug from {models_to_try} responded successfully. "
                f"Last error: {last_exc}"
            )

    async def _stream_from_model(self, model_slug: str, request: NexusRequest) -> AsyncIterator[Token]:
        """Stream from one specific model slug. Raises _ModelNotAvailable if slug fails specifically."""
        ...

    async def complete(self, request: NexusRequest) -> NexusResponse:
        ...

    def count_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def available_context(self) -> int:
        return self.context_window

    def estimated_cost(self, request: NexusRequest) -> dict:
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self) -> HealthStatus:
        """
        GET /models endpoint with auth. Doesn't burn the rate limit budget
        the way a real completion would.
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{OPENROUTER_API_BASE}/models", headers=self._headers)
            if r.status_code == 200:
                return HealthStatus(healthy=True, last_checked=datetime.now(timezone.utc))
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(timezone.utc),
                detail=f"GET /models returned {r.status_code}",
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(timezone.utc),
                detail=str(e),
            )


class _ModelNotAvailable(Exception):
    """Internal: this specific slug is gone, try the next one."""
```

## Critical implementation notes

1. **`HTTP-Referer` and `X-Title` headers matter.** OpenRouter's free tier requires them; without them you may get rate-limited differently or refused. Set them.

2. **`OPENROUTER_QUOTA` is special — it affects two rungs.** The adapter doesn't know about rungs; it just raises `AdapterQuotaExhausted` with a marker. The cascade engine (P2a-T08) maps that to "mark both Rungs 3 and 4 exhausted." Keep the adapter rung-agnostic.

3. **Fallback model slugs.** If GLM-5.1 is removed or renamed, the adapter shouldn't fail catastrophically — it should try the next slug in `fallback_models`. The cascade config lists fallback slugs; the adapter receives them at construction time.

4. **SSE parsing.** OpenRouter streams `data: {json}\n\n` chunks. Each chunk has a `choices[0].delta.content` string. Final chunk is `data: [DONE]`. Ignore keepalive `: ping` lines.

5. **Free models can be slow.** Timeout 180s is generous on purpose. If a free model just hangs, the cascade will fail it over via `SOFT_DEGRADE`; the adapter only needs to surface what it sees.

6. **Don't share an HTTP client across instances.** Two `OpenRouterAdapter` instances (one for Rung 3, one for Rung 4) should each get their own `httpx.AsyncClient`. They can share the underlying connection pool via httpx defaults if needed; don't optimize prematurely.

## Tests

`tests/test_openrouter_adapter.py` — use `respx` to mock httpx:

- `complete_stream` yields tokens from a fixture SSE response.
- HTTP 429 with `{"error":{"message":"...quota..."}}` raises `AdapterQuotaExhausted`.
- HTTP 429 with non-quota message raises `AdapterRateLimit`.
- HTTP 401/403 raises `AdapterUnavailable`.
- HTTP 5xx after 2 retries raises `AdapterUnavailable`.
- Refusal pattern in stream raises `AdapterRefusal`.
- Timeout raises `AdapterTimeout`.
- Primary model slug returning 404 falls through to `fallback_models[0]`.
- All fallback models failing raises `AdapterUnavailable` with all attempted slugs in the message.
- `count_tokens("hello world")` returns ~3.
- `health_check` returns `healthy=True` on 200, `False` on others.
- Constructing without `OPENROUTER_API_KEY` raises `ValueError`.

## Definition of done

- [ ] Verification step completed; current slugs and limits in `OPEN_QUESTIONS.md`.
- [ ] `openrouter.py` exists, conforms to the protocol.
- [ ] Tests pass.
- [ ] Manual smoke test (Rung 3): with `OPENROUTER_API_KEY` exported, instantiate with the verified GLM slug, send "hello world", get a streamed response.
- [ ] Manual smoke test (Rung 4): same, with Qwen3-Coder slug.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't add to `cascade.yaml` yet — P2a-T08.
- Don't implement the "OPENROUTER_QUOTA marks both rungs" logic — that's the cascade engine's job. The adapter only raises the exception.
- Don't try to be clever about which free models are "best" — read from config.
