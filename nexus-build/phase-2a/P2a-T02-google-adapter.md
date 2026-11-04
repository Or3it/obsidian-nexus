# P2a-T02 — Google Adapter (Gemini 3.1 Pro via AI Studio Free Tier)

## Goal

Implement the Rung 2 adapter: Gemini 3.1 Pro via Google AI Studio's free tier API. The user has a free-tier API key already.

## Read first

- `shared/adapter-protocol.md` (full protocol + new error types)
- `shared/cascade-contract.md`
- `OPEN_QUESTIONS.md` for any drift

## Verification step — HARD GATE — DO BEFORE WRITING CODE

1. Verify the current Google AI Studio API endpoint and the correct Python SDK package name. Run:

   ```bash
   pip show google-generativeai
   ```

   If not installed, check current install instructions at https://ai.google.dev/gemini-api/docs/quickstart and use the actually-current package name. The PRD references `google-generativeai>=0.7` but Google may have released the next-gen package (`google-genai`) — verify.

2. Confirm Gemini 3.1 Pro's actual model string. Check https://ai.google.dev/gemini-api/docs/models — the PRD writes `gemini-3-1-pro`. Verify and use what you find.

3. Confirm free-tier rate limits at https://ai.google.dev/pricing — the PRD claims ~15 RPM, ~1500 RPD on Gemini 3.1 Pro. Capture current numbers in `OPEN_QUESTIONS.md`.

4. Confirm the streaming response format (SSE? chunked JSON? SDK abstracts it?). Capture sample output to `tests/fixtures/google_stream.json`.

Append all findings to `OPEN_QUESTIONS.md` under a new dated entry.

## Files to create or replace

```
nexus/orchestrator/adapters/google.py
nexus/tests/test_google_adapter.py
nexus/tests/fixtures/google_stream.json
```

Add to `pyproject.toml`:

```toml
"google-genai>=0.3",     # or whatever the verified-current package name is
```

(Update with the actual package name from the verification step.)

## `nexus/orchestrator/adapters/google.py`

```python
import asyncio
import os
from datetime import datetime, timezone
from typing import AsyncIterator

from ..types import NexusRequest, NexusResponse, Token, HealthStatus
from .base import (
    AdapterUnavailable,
    AdapterQuotaExhausted,
    AdapterTimeout,
    AdapterRefusal,
    AdapterRateLimit,
    AdapterContextOverflow,
)

class GoogleAdapter:
    name = "google"

    def __init__(
        self,
        model: str = "gemini-3-1-pro",       # confirm via verification
        api_key_env: str = "GOOGLE_AI_STUDIO_API_KEY",
        context_window: int = 1_000_000,
        max_output_tokens: int = 8192,
        timeout_seconds: int = 120,
        thinking_level: str = "medium",       # low | medium | high
    ):
        self.model = model
        self.api_key = os.environ.get(api_key_env)
        if not self.api_key:
            raise ValueError(
                f"{api_key_env} environment variable not set. "
                f"Sign up at https://aistudio.google.com/apikey and export the key."
            )
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout_seconds
        self.thinking_level = thinking_level

    async def complete(self, request: NexusRequest) -> NexusResponse:
        ...

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """
        Stream tokens from Gemini. Translate provider errors to AdapterError subclasses:
          - HTTP 429 with quota body → AdapterQuotaExhausted
          - HTTP 429 with rate-limit body → AdapterRateLimit
          - HTTP 4xx auth → AdapterUnavailable
          - HTTP 5xx after retries → AdapterUnavailable
          - Timeout → AdapterTimeout
          - Safety/policy block → AdapterRefusal
          - Prompt too large → AdapterContextOverflow
        """
        ...

    def count_tokens(self, text: str) -> int:
        """
        Gemini provides a token-counting endpoint. Use the SDK's countTokens method.
        Fall back to a char-ratio estimate (~3.5 chars/token for Gemini) if the
        SDK call fails or is too slow.
        """
        ...

    def available_context(self) -> int:
        return self.context_window

    def estimated_cost(self, request: NexusRequest) -> dict:
        # Free tier: no USD cost. The cost is rate-limit consumption.
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self) -> HealthStatus:
        """
        Send a tiny 1-token completion request as a health probe. Cache result
        for 5 minutes to avoid burning rate limit on health checks.
        """
        ...
```

## Critical implementation notes

1. **API key via environment variable, not config file.** The cascade.yaml says `auth_mode: api_key_free_tier` and references the env var name. The actual key lives in the user's shell or in a `.env` file the orchestrator loads at startup. Do NOT write the key into the vault.

2. **Streaming has to be real streaming.** If you use the synchronous SDK, the chat view will hang until the full response arrives. Use the async streaming endpoint. The SDK either supports `async for chunk in response` natively or returns an iterable you wrap in `asyncio.to_thread`.

3. **Translate safety blocks to `AdapterRefusal`.** Gemini returns a "safety_ratings" or "blocked_categories" payload when it refuses. That's a refusal, not an error — map it explicitly.

4. **Token counting via SDK, not estimation.** Gemini's `count_tokens` method is real and free. Use it. Only fall back to estimation if the call fails.

5. **Health check caching.** Don't burn ~5 of your 1500-per-day requests on health checks during normal operation. Cache for 5 minutes.

## Tests

`tests/test_google_adapter.py` — mock the SDK at the boundary:

- `complete_stream` yields tokens in order from a fixture response.
- HTTP 429 with quota-style body raises `AdapterQuotaExhausted`.
- HTTP 429 with non-quota 429 (rate limit) raises `AdapterRateLimit`.
- HTTP 5xx after 2 retries raises `AdapterUnavailable`.
- Safety-block response raises `AdapterRefusal`.
- Prompt exceeding context window raises `AdapterContextOverflow`.
- `count_tokens` returns a sensible number for a known string.
- `health_check` returns `healthy=True` when the API responds, caches for 5 minutes.
- Constructing without an API key raises `ValueError` at init time.

## Definition of done

- [ ] Verification step completed; findings in `OPEN_QUESTIONS.md`.
- [ ] `google.py` exists and conforms to the protocol.
- [ ] All tests pass against mocked SDK.
- [ ] Manual smoke test: with `GOOGLE_AI_STUDIO_API_KEY` exported, run:
  ```bash
  python -c "
  import asyncio
  from orchestrator.adapters.google import GoogleAdapter
  from orchestrator.types import NexusRequest, NexusMessage, StyleProfile
  a = GoogleAdapter()
  r = NexusRequest(thread_id='test', messages=[NexusMessage(role='user', content='Say hello in 5 words.')], style_profile=StyleProfile())
  print(asyncio.run(a.complete(r)))
  "
  ```
  Receives a real Gemini response. Document the invocation in `tests/manual_smoke.md`.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't wire the Google adapter into the orchestrator yet — that's P2a-T09.
- Don't add it to `cascade.yaml` yet — that happens with the cascade engine in P2a-T08.
- Don't implement provider-specific tool-calling. Phase 2b adds web tools generically.
