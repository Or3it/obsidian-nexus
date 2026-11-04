# Adapter Protocol

**Status:** Contract. The Anthropic adapter (Phase 1) and all four others (Phase 2) implement this.

## Protocol

```python
from typing import Protocol, AsyncIterator
from .types import NexusRequest, NexusResponse, Token, HealthStatus

class Adapter(Protocol):
    name: str                              # e.g. "anthropic", "google", "openrouter", "ollama"

    async def complete(self, request: NexusRequest) -> NexusResponse:
        """Single-shot completion. Used when streaming is not needed (e.g., summariser)."""

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """Streamed completion. Default path for user-facing turns."""

    def count_tokens(self, text: str) -> int:
        """Adapter-specific token count. NEVER use a generic estimator across adapters."""

    def available_context(self) -> int:
        """Max input tokens for the currently-selected model. Read from config."""

    def estimated_cost(self, request: NexusRequest) -> dict:
        """Return: {'pro_quota_pct': float, 'usd': float}. Either field may be 0.0."""

    async def health_check(self) -> HealthStatus:
        """Return current adapter health. Should not raise; encode failures in the return value."""
```

## Core types

```python
# orchestrator/types.py

from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime

class StyleProfile(BaseModel):
    format: Literal["prose", "structured", "mixed"] = "prose"
    verbosity: Literal["concise", "default", "detailed"] = "default"
    posture: Literal["assistant", "peer", "tutor"] = "assistant"

class NexusMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class NexusRequest(BaseModel):
    thread_id: str
    messages: list[NexusMessage]
    style_profile: StyleProfile
    model_hint: str | None = None          # adapter may honour or ignore
    max_output_tokens: int | None = None
    stream: bool = True

class NexusResponse(BaseModel):
    content: str
    model_id: str
    auth_mode: Literal["subscription", "api_key_free_tier", "api_key_paid", "local"]
    estimated_tokens: int                   # adapter's best estimate; may approximate
    duration_ms: int
    finish_reason: Literal["stop", "length", "refusal", "error"]
    raw_metadata: dict = Field(default_factory=dict)   # adapter-specific extras

class Token(BaseModel):
    text: str
    is_final: bool = False

class HealthStatus(BaseModel):
    healthy: bool
    last_checked: datetime
    detail: str = ""
```

## Behaviour rules

1. **Adapters never write to the vault.** They produce `NexusResponse` / `Token` stream; the orchestrator owns the vault.
2. **Adapters never raise on expected failures.** Quota exhausted, network down, refusal — encode in `finish_reason="error"` plus `raw_metadata` and let the orchestrator's trigger taxonomy classify.
3. **Unexpected exceptions propagate.** A programmer error (TypeError, key not in dict, etc.) should bubble up. The orchestrator catches and logs; do not swallow.
4. **`count_tokens` may approximate.** The Anthropic adapter in subscription mode has no token meter; it approximates via `chars / chars_per_token_ratio` where the ratio is in `Config/tokenizer_ratios.yaml`. Other adapters use the provider SDK's counter where available.
5. **`available_context` reads config, not hardcoded.** A model upgrade should be a config change, not a code change.
6. **Streaming buffers tokens.** If the network drops mid-stream, the partial response is preserved in a buffer; the orchestrator decides whether to retry, fail over, or surface to user.
7. **No adapter calls another adapter.** Composition (e.g., "use Haiku to summarise for handover") is the orchestrator's responsibility, not the adapter's.

## Phase 1 scope (Anthropic adapter only)

The Phase 1 Anthropic adapter:

- Supports only `claude-sonnet-4-6` (default model). Opus/Haiku land in later phases via the same adapter.
- Authenticates via Claude Code via Ruflo subprocess by default.
- Falls back to direct Claude Code invocation when `use_ruflo: false`.
- `count_tokens` approximates from char count using `Config/tokenizer_ratios.yaml` (default 3.8 chars/token for Sonnet 4.6).
- `estimated_cost` returns `{"pro_quota_pct": <approx>, "usd": 0.0}` in subscription mode.
- `health_check` runs `claude --version` (or `ruflo --version`) with a 5-second timeout.

See `phase-1/P1-T05-anthropic-adapter.md` for full implementation detail.

## Error type hierarchy (Phase 2a additions)

The `AdapterError` subclasses defined in P1-T05 are extended:

- `AdapterError` (base)
  - `AdapterUnavailable` — process failed, network down, auth rejected
  - `AdapterQuotaExhausted` — provider explicitly signaled quota hit
  - `AdapterTimeout` — request exceeded configured timeout
  - `AdapterRefusal` — model returned policy-style refusal
  - `AdapterRateLimit` — HTTP 429 with `Retry-After` (new in Phase 2a)
  - `AdapterContextOverflow` — prompt exceeds adapter's context window (new in Phase 2a)

Each adapter MUST raise the specific subclass, never the base `AdapterError`. The cascade's trigger classifier dispatches on the subclass.

Note: this supersedes Phase 1 Behaviour Rule 2 ("encode in `finish_reason="error"`"). Phase 2a adapters raise; the cascade catches and classifies.

## `selection_hint` field on `NexusRequest`

`NexusRequest` gains an optional `selection_hint: str | None` field used by the cascade engine for routing decisions. Phase 2a uses it only for pin-passthrough (`"pin:gemini-3-1-pro"`). Phase 2b will use it for Opus routing.

```python
class NexusRequest(BaseModel):
    thread_id: str
    messages: list[NexusMessage]
    style_profile: StyleProfile
    model_hint: str | None = None
    selection_hint: str | None = None       # new in Phase 2a
    max_output_tokens: int | None = None
    stream: bool = True
```
