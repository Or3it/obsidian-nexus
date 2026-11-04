# P2c-T03 — ModelHealth Ledger (Exponential Backoff)

## Goal

Track per-model health with exponential backoff so a model that fails is skipped for a growing window and retried automatically — all without spending quota on probing. Extends the existing CooldownLedger concept; does not replace it (the shared `openrouter_shared` quota cooldown still lives in the cooldown ledger).

## Read first

- `nexus/orchestrator/cascade/cooldown.py` (CooldownLedger: mark_exhausted / is_exhausted, JSON persistence)
- `nexus/orchestrator/adapters/base.py` (AdapterModelUnavailable from T01, AdapterRateLimit, AdapterQuotaExhausted)

## Design

Two distinct concerns, kept separate:
- **Quota cooldown** (existing `CooldownLedger`): account-wide OpenRouter daily quota, keyed `openrouter_shared`. Long. Unchanged.
- **Model health** (new `ModelHealth`): per-model availability, keyed by model id. Exponential backoff. This ticket.

### Backoff schedule

```
failure 1 → dead for 5 min
failure 2 → dead for 15 min
failure 3 → dead for 60 min
failure 4+ → dead for 60 min (capped)
success   → clear dead mark, reset failure counter to 0
```

Rate-limit failures (`AdapterRateLimit`) use the exception's own `cooldown_seconds` (~45s) and do NOT increment the exponential failure counter — they're transient throttles, not the model being down. Only `AdapterModelUnavailable` drives the exponential schedule.

## Files to create / modify

```
nexus/orchestrator/cascade/model_health.py    ← new
nexus/tests/test_model_health.py              ← new
```

## `model_health.py`

```python
"""Per-model health tracking with exponential backoff. Persisted to JSON
alongside the cooldown ledger. Quota-free: state changes only on real
turn outcomes, never on proactive probing."""

import json
from dataclasses import dataclass, asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

_BACKOFF_MINUTES = [5, 15, 60]          # index by (failures-1), clamp at last
_CAP_MINUTES = 60


@dataclass
class _Entry:
    consecutive_failures: int = 0
    dead_until: str | None = None        # ISO8601, or None if live
    last_failure: str | None = None
    last_success: str | None = None
    last_reason: str | None = None


class ModelHealth:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, _Entry] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self._data = {k: _Entry(**v) for k, v in raw.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: asdict(v) for k, v in self._data.items()},
                       indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def is_dead(self, model: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        e = self._data.get(model)
        if not e or not e.dead_until:
            return False
        return now < datetime.fromisoformat(e.dead_until)

    def mark_unavailable(self, model: str, reason: str = "",
                         now: datetime | None = None) -> int:
        """Record a model-unavailable failure. Returns minutes marked dead."""
        now = now or datetime.now(UTC)
        e = self._data.setdefault(model, _Entry())
        e.consecutive_failures += 1
        idx = min(e.consecutive_failures - 1, len(_BACKOFF_MINUTES) - 1)
        minutes = min(_BACKOFF_MINUTES[idx], _CAP_MINUTES)
        e.dead_until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        e.last_failure = now.isoformat(timespec="seconds")
        e.last_reason = reason[:200]
        self._save()
        return minutes

    def mark_success(self, model: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        e = self._data.setdefault(model, _Entry())
        e.consecutive_failures = 0
        e.dead_until = None
        e.last_success = now.isoformat(timespec="seconds")
        e.last_reason = None
        self._save()

    def status(self, model: str, now: datetime | None = None) -> dict:
        """For the UI / transparency layer (T06)."""
        now = now or datetime.now(UTC)
        e = self._data.get(model)
        if not e:
            return {"model": model, "state": "untested"}
        if e.dead_until and now < datetime.fromisoformat(e.dead_until):
            return {"model": model, "state": "dead",
                    "until": e.dead_until, "reason": e.last_reason,
                    "failures": e.consecutive_failures}
        return {"model": model, "state": "live",
                "last_success": e.last_success}

    def all_status(self, models: list[str], now: datetime | None = None) -> list[dict]:
        return [self.status(m, now) for m in models]
```

Store the file next to the cooldown ledger, e.g. `Orchestrator/Config/model_health.json` (gitignored like budgets).

## Integration note (consumed in T04)

The engine, after each turn outcome, calls:
- `mark_unavailable(model, reason)` when the adapter raised `AdapterModelUnavailable`
- `mark_success(model)` when the turn streamed successfully
- For `AdapterRateLimit`: use the existing cooldown ledger with the exception's `cooldown_seconds`, do NOT touch ModelHealth's failure counter
- For `AdapterQuotaExhausted`: existing shared cooldown, do NOT touch ModelHealth

This ticket only builds and unit-tests the ledger. T04 wires it in.

## Tests

`tests/test_model_health.py` (use injected `now` for determinism):
- Fresh model → `is_dead` False, status "untested".
- One `mark_unavailable` → dead 5 min; `is_dead` True before, False after (advance `now`).
- Two failures → 15 min; three → 60 min; four → still 60 (cap).
- `mark_success` clears dead_until and resets failure counter to 0.
- Backoff resets: fail→fail→success→fail goes back to 5 min, not 60.
- Persistence: write entries, construct a new `ModelHealth` on the same path, state survives.
- `status` returns "dead" with reason/until while dead, "live" after success.
- `all_status` returns one entry per requested model.

## Definition of done

- [ ] `model_health.py` exists with the API above.
- [ ] All tests pass (deterministic via injected `now`).
- [ ] File persists to `Orchestrator/Config/model_health.json`; path is gitignored.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't wire into the engine — T04.
- Don't add proactive probing — backoff expiry + real-turn retry is the mechanism. T05 adds the single optional recovery probe.
- Don't merge with CooldownLedger — they track different things and stay separate.
