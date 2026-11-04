"""Adapter protocol and base exception hierarchy."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..types import HealthStatus, NexusRequest, NexusResponse, Token


@runtime_checkable
class Adapter(Protocol):
    name: str

    async def complete(self, request: NexusRequest) -> NexusResponse:
        """Single-shot completion. Used when streaming is not needed."""
        ...

    def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """Streamed completion. Default path for user-facing turns."""
        ...

    def count_tokens(self, text: str) -> int:
        """Adapter-specific token count. Never use a generic estimator across adapters."""
        ...

    def available_context(self) -> int:
        """Max input tokens for the currently-selected model. Read from config."""
        ...

    def estimated_cost(self, request: NexusRequest) -> dict:  # type: ignore[type-arg]
        """Return: {'pro_quota_pct': float, 'usd': float}. Either field may be 0.0."""
        ...

    async def health_check(self) -> HealthStatus:
        """Return current adapter health. Should not raise; encode failures in return value."""
        ...


class AdapterError(Exception):
    """Adapter-level error. Subclasses describe the failure class."""


class AdapterUnavailable(AdapterError):
    """The adapter's backend is unreachable or returned a non-zero exit."""


class AdapterQuotaExhausted(AdapterError):
    """Pro quota or API budget limit reached.

    cooldown_key groups multiple adapter rungs under one shared cooldown.
    OpenRouter adapters set cooldown_key='openrouter_shared' so the cascade
    engine cools down all three OpenRouter rungs together when the shared
    daily limit is hit (P2a-T08 reads this attribute).
    """

    cooldown_key: str | None

    def __init__(self, message: str, cooldown_key: str | None = None) -> None:
        super().__init__(message)
        self.cooldown_key = cooldown_key


class AdapterTimeout(AdapterError):
    """Subprocess or network call exceeded the configured timeout."""


class AdapterRefusal(AdapterError):
    """Model refused the request (content policy, etc.)."""


class AdapterRateLimit(AdapterError):
    """HTTP 429 with Retry-After — per-minute rate limit, not daily quota exhaustion."""

    def __init__(self, message: str, cooldown_seconds: int = 45) -> None:
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds


class AdapterModelUnavailable(AdapterError):
    """The specific model/provider is unavailable (404 / provider error).
    Distinct from rate-limit and quota: the model itself is down, not throttled.
    Carries cooldown_seconds so T03 knows how long to skip this model."""

    def __init__(self, message: str, cooldown_seconds: int = 300) -> None:
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds


class AdapterContextOverflow(AdapterError):
    """Prompt exceeds the adapter's context window."""
