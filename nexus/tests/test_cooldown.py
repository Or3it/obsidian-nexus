"""Tests for cascade/cooldown.py — CooldownLedger."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from orchestrator.cascade.cooldown import CooldownLedger

# ---------------------------------------------------------------------------
# is_exhausted — basic states
# ---------------------------------------------------------------------------


class TestIsExhausted:
    def test_unknown_key_not_exhausted(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "budgets.yaml")
        assert not ledger.is_exhausted("rung_1_anthropic")

    def test_mark_then_is_exhausted(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "budgets.yaml")
        ledger.mark_exhausted("rung_1_anthropic", 3600)
        assert ledger.is_exhausted("rung_1_anthropic")

    def test_expired_cooldown_not_exhausted(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "budgets.yaml")
        ledger.mark_exhausted("rung_1_anthropic", 60)
        future = datetime.now(UTC) + timedelta(seconds=120)
        with patch("orchestrator.cascade.cooldown._now", return_value=future):
            assert not ledger.is_exhausted("rung_1_anthropic")

    def test_within_cooldown_still_exhausted(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "budgets.yaml")
        ledger.mark_exhausted("rung_1_anthropic", 3600)
        soon = datetime.now(UTC) + timedelta(seconds=30)
        with patch("orchestrator.cascade.cooldown._now", return_value=soon):
            assert ledger.is_exhausted("rung_1_anthropic")

    def test_different_keys_are_independent(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "budgets.yaml")
        ledger.mark_exhausted("rung_1_anthropic", 3600)
        assert not ledger.is_exhausted("openrouter_shared")


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_removes_cooldown(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "budgets.yaml")
        ledger.mark_exhausted("rung_1_anthropic", 3600)
        ledger.clear("rung_1_anthropic")
        assert not ledger.is_exhausted("rung_1_anthropic")

    def test_clear_nonexistent_key_is_safe(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "budgets.yaml")
        ledger.clear("nonexistent_key")  # must not raise


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_survives_restart(self, tmp_path) -> None:
        path = tmp_path / "budgets.yaml"
        ledger1 = CooldownLedger(path)
        ledger1.mark_exhausted("openrouter_shared", 3600)

        ledger2 = CooldownLedger(path)
        assert ledger2.is_exhausted("openrouter_shared")

    def test_missing_file_is_treated_as_empty(self, tmp_path) -> None:
        ledger = CooldownLedger(tmp_path / "nonexistent.yaml")
        assert not ledger.is_exhausted("anything")

    def test_cleared_key_not_present_after_reload(self, tmp_path) -> None:
        path = tmp_path / "budgets.yaml"
        ledger1 = CooldownLedger(path)
        ledger1.mark_exhausted("rung_1_anthropic", 3600)
        ledger1.clear("rung_1_anthropic")

        ledger2 = CooldownLedger(path)
        assert not ledger2.is_exhausted("rung_1_anthropic")

    def test_parent_dirs_created_automatically(self, tmp_path) -> None:
        deep_path = tmp_path / "a" / "b" / "budgets.yaml"
        ledger = CooldownLedger(deep_path)
        ledger.mark_exhausted("k", 10)
        assert deep_path.exists()
