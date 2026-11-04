# P2a-T06 — Trigger Taxonomy + Detector

## Goal

A single module that takes an `AdapterError` (or successful response with metadata) and returns a trigger class from the seven-class taxonomy. The cascade engine (P2a-T08) uses this to decide what to do next.

## Read first

- `shared/trigger-taxonomy.md` (the canonical taxonomy)
- `shared/adapter-protocol.md` (the AdapterError subclasses)

## Files to create

```
nexus/orchestrator/cascade/__init__.py
nexus/orchestrator/cascade/triggers.py
nexus/tests/test_triggers.py
```

(`cascade/` is a new package. The engine itself lands in P2a-T08; this ticket creates the package and just the triggers module.)

## `nexus/orchestrator/cascade/triggers.py`

```python
"""
Trigger taxonomy classifier.

Maps adapter errors and successful-but-unusual responses to one of the seven
trigger classes. The cascade engine dispatches on the trigger class to decide
whether to retry, fail over, or surface to user.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import time

from ..adapters.base import (
    AdapterError,
    AdapterUnavailable,
    AdapterQuotaExhausted,
    AdapterTimeout,
    AdapterRefusal,
    AdapterRateLimit,
    AdapterContextOverflow,
)


class TriggerClass(str, Enum):
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
    retryable: bool                            # Should the cascade retry on the SAME rung?
    failover: bool                             # Should the cascade move to the next rung?
    cooldown_seconds: int | None = None        # If set, mark this rung exhausted for N seconds
    cross_rung_cooldown: bool = False          # If True, cooldown applies to a sibling rung too
    detail: str = ""                            # Human-readable; flows into handover doc


def classify_error(
    exc: AdapterError,
    adapter_name: str,
) -> TriggerOutcome:
    """
    Map an adapter exception to a trigger class.

    `adapter_name` matters: `AdapterQuotaExhausted` from Anthropic is PRO_QUOTA_EXHAUSTED,
    but from OpenRouter is OPENROUTER_QUOTA, and the latter cools down two rungs.
    """
    # Anthropic-specific
    if adapter_name == "anthropic":
        if isinstance(exc, AdapterQuotaExhausted):
            return TriggerOutcome(
                trigger=TriggerClass.PRO_QUOTA_EXHAUSTED,
                retryable=False,
                failover=True,
                cooldown_seconds=_seconds_until_next_pro_reset(),
                detail=f"Anthropic Pro quota exhausted: {exc}",
            )

    # OpenRouter-specific
    if adapter_name == "openrouter":
        if isinstance(exc, AdapterQuotaExhausted):
            return TriggerOutcome(
                trigger=TriggerClass.OPENROUTER_QUOTA,
                retryable=False,
                failover=True,
                cooldown_seconds=_seconds_until_next_openrouter_reset(),
                cross_rung_cooldown=True,         # affects sibling OpenRouter rung
                detail=f"OpenRouter daily quota exhausted: {exc}",
            )

    # Universal mappings
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
            failover=False,                       # one retry first; cascade will failover if retry fails
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
            retryable=True,                       # one retry on same model first
            failover=True,
            detail=f"{adapter_name} refused: {exc}",
        )

    # Unknown adapter error — treat as HARD_FAIL conservatively
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
    """
    Pro weekly quotas reset on Mondays at 00:00 UTC (verify against Anthropic docs).
    Phase 2a uses a conservative 7-day cooldown; Phase 4 can be smarter.
    """
    return 7 * 24 * 3600


def _seconds_until_next_openrouter_reset() -> int:
    """
    OpenRouter daily quotas reset at 00:00 UTC. Compute time until that.
    """
    now = datetime.utcnow()
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if tomorrow <= now:
        from datetime import timedelta
        tomorrow = tomorrow + timedelta(days=1)
    return int((tomorrow - now).total_seconds())


class LatencyTracker:
    """
    Tracks rolling P95 latency per adapter to detect SOFT_DEGRADE.

    Stateless across orchestrator restarts (intentional — short windows only).
    """
    def __init__(self, window_size: int = 20, degrade_multiplier: float = 3.0):
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
        """
        Returns True if the latest sample is >3x the rolling P95 for 2 consecutive
        observations. The cascade then triggers a SOFT_DEGRADE failover.
        """
        samples = self._samples.get(adapter_name, [])
        if len(samples) < self._window:
            return False
        p95 = sorted(samples)[int(len(samples) * 0.95)]
        if latest_ms > p95 * self._multiplier:
            self._consecutive_slow[adapter_name] = self._consecutive_slow.get(adapter_name, 0) + 1
            return self._consecutive_slow[adapter_name] >= 2
        else:
            self._consecutive_slow[adapter_name] = 0
            return False
```

## Critical implementation notes

1. **The classifier knows about adapter names.** `AdapterQuotaExhausted` doesn't know if it came from Anthropic or OpenRouter; the cascade engine passes the adapter's name in. Keep the adapter classes provider-agnostic.

2. **`cross_rung_cooldown` is the OpenRouter special case.** When Rung 3 (GLM) raises `AdapterQuotaExhausted` because the OpenRouter daily limit is hit, Rung 4 (Qwen3/DeepSeek) shares that limit. The cascade engine (P2a-T08) reads `cross_rung_cooldown=True` and marks both rungs cooled-down.

3. **Cooldown precision is intentionally rough.** "7 days for Pro" is a placeholder until we know how Anthropic actually surfaces the reset timestamp. If the adapter can extract a real reset time from the error response, the cooldown can be precise; otherwise this approximation is fine — worst case, the user manually unpins.

4. **`LatencyTracker` is stateless across restarts.** Don't persist this to disk. The window is short (20 requests) and rebuilds quickly. Persisting introduces stale-data bugs after a long downtime.

5. **No imports from `cascade/engine.py` yet.** That file doesn't exist until P2a-T08. The triggers module is self-contained.

## Tests

`tests/test_triggers.py`:

- `AdapterQuotaExhausted` from `anthropic` → `PRO_QUOTA_EXHAUSTED` with ~7-day cooldown, no retry, failover.
- `AdapterQuotaExhausted` from `openrouter` → `OPENROUTER_QUOTA` with cross-rung cooldown, no retry, failover.
- `AdapterQuotaExhausted` from `google` → falls through to HARD_FAIL (no special path).
- `AdapterUnavailable` from any adapter → `HARD_FAIL`, no retry, failover.
- `AdapterRateLimit` → `SOFT_DEGRADE`, retryable, no failover (yet).
- `AdapterTimeout` → `SOFT_DEGRADE`, no retry, failover.
- `AdapterContextOverflow` → `CONTEXT_LIMIT`, no retry, failover.
- `AdapterRefusal` → `EXPLICIT_REFUSAL`, retryable (once), failover.
- Unknown subclass of `AdapterError` → `HARD_FAIL` conservatively.
- `manual_trigger()` returns `MANUAL`.
- `LatencyTracker`: insufficient samples → never degrading.
- `LatencyTracker`: one slow sample after stable baseline → not degrading (need 2 consecutive).
- `LatencyTracker`: two consecutive slow samples → degrading.
- `LatencyTracker`: fast sample resets the consecutive counter.
- `_seconds_until_next_openrouter_reset` returns a sensible number between 0 and 86400.

## Definition of done

- [ ] `cascade/__init__.py` and `cascade/triggers.py` exist.
- [ ] All tests pass.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't implement the cascade engine itself. P2a-T08.
- Don't wire to the orchestrator. P2a-T09.
- Don't persist cooldown state to `Config/budgets.yaml` yet. P2a-T08 owns that file. This ticket only computes the cooldown durations.
- Don't try to parse provider-specific error response bodies for precise reset timestamps. The conservative defaults are fine; precision is a Phase 4 polish item.
