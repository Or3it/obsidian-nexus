"""Rung cooldown state. Persisted to Config/budgets.yaml so quota exhaustion
survives orchestrator restarts.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


def _now() -> datetime:
    """Return current UTC time. Separate function so tests can patch it."""
    return datetime.now(UTC)


class CooldownLedger:
    """Tracks per-rung cooldowns. Stateful; reads/writes Config/budgets.yaml."""

    def __init__(self, budgets_path: Path) -> None:
        self.path = budgets_path
        self._state: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._state, f, sort_keys=False, allow_unicode=True)

    def mark_exhausted(self, key: str, seconds: int) -> None:
        """Mark key as exhausted for `seconds` seconds.

        key is a rung identifier such as 'rung_1_anthropic' or 'openrouter_shared'.
        """
        now = _now()
        until = now + timedelta(seconds=seconds)
        self._state[key] = {
            "exhausted_until": until.isoformat(timespec="seconds"),
            "last_seen": now.isoformat(timespec="seconds"),
        }
        self._save()

    def is_exhausted(self, key: str) -> bool:
        record = self._state.get(key)
        if not record:
            return False
        until_str = record.get("exhausted_until", "")
        if not until_str:
            return False
        until = datetime.fromisoformat(str(until_str))
        return _now() < until

    def clear(self, key: str) -> None:
        if key in self._state:
            del self._state[key]
            self._save()
