"""
Trigger taxonomy classifier.

Maps adapter errors and successful-but-unusual responses to one of the seven
trigger classes. The cascade engine dispatches on the trigger class to decide
whether to retry, fail over, or surface to user.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ..adapters.base import (
    AdapterContextOverflow,
    AdapterError,
    AdapterQuotaExhausted,
    AdapterRateLimit,
    AdapterRefusal,
    AdapterTimeout,
    AdapterUnavailable,
)


class TriggerClass(StrEnum):
    HARD_FAIL = "HARD_FAIL"
    PRO_QUOTA_EXHAUSTED = "PRO_QUOTA_EXHAUSTED"
    SOFT_DEGRADE = "SOFT_DEGRADE"
    OPENROUTER_QUOTA = "OPENROUTER_QUOTA"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    EXPLICIT_REFUSAL = "EXPLICIT_REFUSAL"
    MANUAL = "MANUAL"


@dataclass
class TriggerOutcome:
    """The classifier's decision for one error event."""

    trigger: TriggerClass
    retryable: bool
    failover: bool
    cooldown_seconds: int | None = None
    cross_rung_cooldown: bool = False
    detail: str = ""


def classify_error(exc: AdapterError, adapter_name: str) -> TriggerOutcome:
    """
    Map an adapter exception to a trigger class.

    `adapter_name` matters: `AdapterQuotaExhausted` from Anthropic is PRO_QUOTA_EXHAUSTED,
    but from OpenRouter is OPENROUTER_QUOTA, and the latter cools down sibling rungs.
    """
    if adapter_name == "anthropic" and isinstance(exc, AdapterQuotaExhausted):
        return TriggerOutcome(
            trigger=TriggerClass.PRO_QUOTA_EXHAUSTED,
            retryable=False,
            failover=True,
            cooldown_seconds=_seconds_until_next_pro_reset(),
            detail=f"Anthropic Pro quota exhausted: {exc}",
        )

    if adapter_name == "openrouter" and isinstance(exc, AdapterQuotaExhausted):
        return TriggerOutcome(
            trigger=TriggerClass.OPENROUTER_QUOTA,
            retryable=False,
            failover=True,
            cooldown_seconds=_seconds_until_next_openrouter_reset(),
            cross_rung_cooldown=True,
            detail=f"OpenRouter daily quota exhausted: {exc}",
        )

    if isinstance(exc, AdapterUnavailable):
        return TriggerOutcome(
            trigger=TriggerClass.HARD_FAIL,
            retryable=False,
            failover=True,
            detail=f"{adapter_name} adapter unavailable: {exc}",
        )
    if isinstance(exc, AdapterRateLimit):
        return TriggerOutcome(
            trigger=TriggerClass.SOFT_DEGRADE,
            retryable=True,
            failover=False,
            cooldown_seconds=30,
            detail=f"{adapter_name} rate-limited; brief cooldown",
        )
    if isinstance(exc, AdapterTimeout):
        return TriggerOutcome(
            trigger=TriggerClass.SOFT_DEGRADE,
            retryable=False,
            failover=True,
            detail=f"{adapter_name} timed out: {exc}",
        )
    if isinstance(exc, AdapterContextOverflow):
        return TriggerOutcome(
            trigger=TriggerClass.CONTEXT_LIMIT,
            retryable=False,
            failover=True,
            detail=f"{adapter_name} prompt exceeded context window",
        )
    if isinstance(exc, AdapterRefusal):
        return TriggerOutcome(
            trigger=TriggerClass.EXPLICIT_REFUSAL,
            retryable=True,
            failover=True,
            detail=f"{adapter_name} refused: {exc}",
        )

    # Unknown AdapterError subclass — treat as HARD_FAIL conservatively
    return TriggerOutcome(
        trigger=TriggerClass.HARD_FAIL,
        retryable=False,
        failover=True,
        detail=f"Unclassified {adapter_name} error: {type(exc).__name__}: {exc}",
    )


def manual_trigger() -> TriggerOutcome:
    """User explicitly invoked a failover or pin change."""
    return TriggerOutcome(
        trigger=TriggerClass.MANUAL,
        retryable=False,
        failover=True,
        detail="User-initiated",
    )


def _seconds_until_next_pro_reset() -> int:
    """Conservative 7-day cooldown for Anthropic Pro weekly quota."""
    return 7 * 24 * 3600


def _seconds_until_next_openrouter_reset() -> int:
    """Time in seconds until OpenRouter's daily quota resets at 00:00 UTC."""
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight <= now:
        midnight = midnight + timedelta(days=1)
    return int((midnight - now).total_seconds())


class LatencyTracker:
    """Rolling P95 latency tracker for SOFT_DEGRADE detection. Stateless across restarts."""

    def __init__(self, window_size: int = 20, degrade_multiplier: float = 3.0) -> None:
        self._samples: dict[str, list[float]] = {}
        self._window = window_size
        self._multiplier = degrade_multiplier
        self._consecutive_slow: dict[str, int] = {}

    def record(self, adapter_name: str, duration_ms: float) -> None:
        samples = self._samples.setdefault(adapter_name, [])
        samples.append(duration_ms)
        if len(samples) > self._window:
            samples.pop(0)

    def is_degrading(self, adapter_name: str, latest_ms: float) -> bool:
        """Returns True if latest_ms is >3x rolling P95 for 2+ consecutive observations."""
        samples = self._samples.get(adapter_name, [])
        if len(samples) < self._window:
            return False
        p95 = sorted(samples)[int(len(samples) * 0.95)]
        if latest_ms > p95 * self._multiplier:
            self._consecutive_slow[adapter_name] = (
                self._consecutive_slow.get(adapter_name, 0) + 1
            )
            return self._consecutive_slow[adapter_name] >= 2
        self._consecutive_slow[adapter_name] = 0
        return False
