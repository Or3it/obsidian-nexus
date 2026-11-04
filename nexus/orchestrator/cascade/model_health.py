"""Per-model health tracking with exponential backoff.

Persisted to Orchestrator/Config/model_health.json alongside the cooldown
ledger. Quota-free: state changes only on real turn outcomes, never on
proactive probing.

Distinct from CooldownLedger:
- CooldownLedger → account-wide quota cooldowns (openrouter_shared, long)
- ModelHealth    → per-model availability via exponential backoff (this file)
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_BACKOFF_MINUTES = [5, 15, 60]  # indexed by (consecutive_failures - 1), clamped at last
_CAP_MINUTES = 60


@dataclass
class _Entry:
    consecutive_failures: int = 0
    dead_until: str | None = None   # ISO 8601, None → live
    last_failure: str | None = None
    last_success: str | None = None
    last_reason: str | None = None


class ModelHealth:
    """Per-model exponential-backoff ledger.

    After ``mark_unavailable`` the model is skipped until ``dead_until``
    expires.  ``mark_success`` clears the dead mark and resets the failure
    counter so the next failure starts the backoff schedule from the beginning.

    All public methods accept an optional ``now`` parameter for deterministic
    testing — production callers omit it and get ``datetime.now(UTC)``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, _Entry] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self.path.exists():
            raw: dict[str, Any] = json.loads(
                self.path.read_text(encoding="utf-8") or "{}"
            )
            self._data = {k: _Entry(**v) for k, v in raw.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {k: asdict(v) for k, v in self._data.items()},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_dead(self, model: str, now: datetime | None = None) -> bool:
        """Return True if the model is currently in its backoff window."""
        now = now or datetime.now(UTC)
        e = self._data.get(model)
        if not e or not e.dead_until:
            return False
        return now < datetime.fromisoformat(e.dead_until)

    def mark_unavailable(
        self, model: str, reason: str = "", now: datetime | None = None
    ) -> int:
        """Record a model-unavailable failure. Returns minutes marked dead.

        Only ``AdapterModelUnavailable`` should drive this — ``AdapterRateLimit``
        is a transient throttle and must NOT increment the failure counter.
        """
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
        """Record a successful turn. Clears the dead mark and resets the counter.

        The reset is essential: fail→fail→success→fail must restart at 5 min,
        not continue the exponential schedule from where it left off.
        """
        now = now or datetime.now(UTC)
        e = self._data.setdefault(model, _Entry())
        e.consecutive_failures = 0
        e.dead_until = None
        e.last_success = now.isoformat(timespec="seconds")
        e.last_reason = None
        self._save()

    def status(self, model: str, now: datetime | None = None) -> dict[str, Any]:
        """Return a status dict for the model (consumed by the UI / T06)."""
        now = now or datetime.now(UTC)
        e = self._data.get(model)
        if not e:
            return {"model": model, "state": "untested"}
        if e.dead_until and now < datetime.fromisoformat(e.dead_until):
            return {
                "model": model,
                "state": "dead",
                "until": e.dead_until,
                "reason": e.last_reason,
                "failures": e.consecutive_failures,
            }
        return {"model": model, "state": "live", "last_success": e.last_success}

    def all_status(
        self, models: list[str], now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Return a status dict for each model in *models* (T06 cascade transparency)."""
        return [self.status(m, now) for m in models]
