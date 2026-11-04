"""Tests for cascade/model_health.py — ModelHealth ledger.

All time-dependent calls use injected ``now`` for full determinism — no
patching required, no wall-clock dependency.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator.cascade.model_health import ModelHealth

# Fixed base time used throughout; chosen to be clearly fake.
T0 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> ModelHealth:
    return ModelHealth(tmp_path / "model_health.json")


MODEL = "test/model:free"


# ---------------------------------------------------------------------------
# Fresh / untested state
# ---------------------------------------------------------------------------


class TestFreshModel:
    def test_is_dead_false_for_unknown_model(self, tmp_path: Path) -> None:
        assert _ledger(tmp_path).is_dead(MODEL, now=T0) is False

    def test_status_untested_for_unknown_model(self, tmp_path: Path) -> None:
        s = _ledger(tmp_path).status(MODEL, now=T0)
        assert s["state"] == "untested"
        assert s["model"] == MODEL


# ---------------------------------------------------------------------------
# Backoff schedule: 5 → 15 → 60 → 60 (cap)
# ---------------------------------------------------------------------------


class TestBackoffSchedule:
    def test_first_failure_returns_5_minutes(self, tmp_path: Path) -> None:
        minutes = _ledger(tmp_path).mark_unavailable(MODEL, "err", now=T0)
        assert minutes == 5

    def test_second_failure_returns_15_minutes(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        minutes = ledger.mark_unavailable(MODEL, "err", now=T0)
        assert minutes == 15

    def test_third_failure_returns_60_minutes(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        for _ in range(2):
            ledger.mark_unavailable(MODEL, "err", now=T0)
        minutes = ledger.mark_unavailable(MODEL, "err", now=T0)
        assert minutes == 60

    def test_fourth_failure_capped_at_60_minutes(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        for _ in range(3):
            ledger.mark_unavailable(MODEL, "err", now=T0)
        minutes = ledger.mark_unavailable(MODEL, "err", now=T0)
        assert minutes == 60

    def test_fifth_failure_still_capped_at_60_minutes(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        for _ in range(4):
            ledger.mark_unavailable(MODEL, "err", now=T0)
        minutes = ledger.mark_unavailable(MODEL, "err", now=T0)
        assert minutes == 60


# ---------------------------------------------------------------------------
# is_dead time window
# ---------------------------------------------------------------------------


class TestIsDeadWindow:
    def test_is_dead_true_immediately_after_failure(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        assert ledger.is_dead(MODEL, now=T0) is True

    def test_is_dead_true_within_5_min_window(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        assert ledger.is_dead(MODEL, now=T0 + timedelta(minutes=4, seconds=59)) is True

    def test_is_dead_false_after_5_min_expires(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        assert ledger.is_dead(MODEL, now=T0 + timedelta(minutes=5, seconds=1)) is False

    def test_is_dead_false_at_exactly_dead_until_boundary(self, tmp_path: Path) -> None:
        """dead_until is exclusive: at exactly dead_until, the model is live."""
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        # dead_until = T0 + 5 min; at exactly T0+5min, now < dead_until is False
        assert ledger.is_dead(MODEL, now=T0 + timedelta(minutes=5)) is False

    def test_is_dead_respects_second_failure_window(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        ledger.mark_unavailable(MODEL, "err", now=T0)  # 15-min window
        # Still dead at 10 min (within 15-min window)
        assert ledger.is_dead(MODEL, now=T0 + timedelta(minutes=10)) is True
        # Live at 16 min
        assert ledger.is_dead(MODEL, now=T0 + timedelta(minutes=16)) is False


# ---------------------------------------------------------------------------
# mark_success — clears dead mark and resets counter
# ---------------------------------------------------------------------------


class TestMarkSuccess:
    def test_success_clears_dead_mark(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        assert ledger.is_dead(MODEL, now=T0) is True
        ledger.mark_success(MODEL, now=T0 + timedelta(minutes=10))
        assert ledger.is_dead(MODEL, now=T0 + timedelta(minutes=10)) is False

    def test_success_on_live_model_is_idempotent(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_success(MODEL, now=T0)  # never failed
        assert ledger.is_dead(MODEL, now=T0) is False

    def test_success_resets_failure_counter(self, tmp_path: Path) -> None:
        """After two failures + success, next failure should be 5 min again."""
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        ledger.mark_success(MODEL, now=T0 + timedelta(minutes=20))
        minutes = ledger.mark_unavailable(MODEL, "err", now=T0 + timedelta(minutes=30))
        assert minutes == 5


# ---------------------------------------------------------------------------
# Backoff reset: fail→fail→success→fail goes back to 5 min, not 60
# ---------------------------------------------------------------------------


class TestBackoffReset:
    def test_fail_fail_success_fail_resets_to_5_min(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)           # → 5 min
        ledger.mark_unavailable(MODEL, "err", now=T0)           # → 15 min
        ledger.mark_success(MODEL, now=T0 + timedelta(minutes=20))  # reset
        minutes = ledger.mark_unavailable(MODEL, "err", now=T0 + timedelta(minutes=30))
        assert minutes == 5  # NOT 60

    def test_backoff_accumulates_independently_after_reset(self, tmp_path: Path) -> None:
        """A second full failure run from scratch still hits the same schedule."""
        ledger = _ledger(tmp_path)
        # First run: 3 failures → hits cap
        for _ in range(3):
            ledger.mark_unavailable(MODEL, "err", now=T0)
        ledger.mark_success(MODEL, now=T0 + timedelta(hours=2))
        # Second run: should restart from 5 min
        m1 = ledger.mark_unavailable(MODEL, "err", now=T0 + timedelta(hours=3))
        m2 = ledger.mark_unavailable(MODEL, "err", now=T0 + timedelta(hours=3))
        m3 = ledger.mark_unavailable(MODEL, "err", now=T0 + timedelta(hours=3))
        assert m1 == 5
        assert m2 == 15
        assert m3 == 60


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_state_survives_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "model_health.json"
        ledger1 = ModelHealth(path)
        ledger1.mark_unavailable(MODEL, "Provider returned error", now=T0)
        # New instance on the same path should restore state
        ledger2 = ModelHealth(path)
        assert ledger2.is_dead(MODEL, now=T0 + timedelta(minutes=1)) is True
        assert ledger2.is_dead(MODEL, now=T0 + timedelta(minutes=6)) is False

    def test_failure_counter_survives_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "model_health.json"
        ledger1 = ModelHealth(path)
        ledger1.mark_unavailable(MODEL, "err", now=T0)
        ledger1.mark_unavailable(MODEL, "err", now=T0)
        ledger2 = ModelHealth(path)
        # Third failure on reloaded ledger should be 60 min
        minutes = ledger2.mark_unavailable(MODEL, "err", now=T0)
        assert minutes == 60

    def test_file_created_in_nonexistent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "model_health.json"
        ledger = ModelHealth(path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        assert path.exists()

    def test_missing_file_starts_empty(self, tmp_path: Path) -> None:
        ledger = ModelHealth(tmp_path / "nonexistent.json")
        assert ledger.is_dead(MODEL, now=T0) is False

    def test_multiple_models_persisted_independently(self, tmp_path: Path) -> None:
        path = tmp_path / "model_health.json"
        ledger1 = ModelHealth(path)
        ledger1.mark_unavailable("model/a:free", "err a", now=T0)
        ledger1.mark_unavailable("model/a:free", "err a", now=T0)  # 2 failures
        ledger1.mark_unavailable("model/b:free", "err b", now=T0)  # 1 failure

        ledger2 = ModelHealth(path)
        # a: 2 failures → next is 60 min
        assert ledger2.mark_unavailable("model/a:free", "err", now=T0) == 60
        # b: 1 failure → next is 15 min
        assert ledger2.mark_unavailable("model/b:free", "err", now=T0) == 15


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_untested_model(self, tmp_path: Path) -> None:
        s = _ledger(tmp_path).status(MODEL, now=T0)
        assert s == {"model": MODEL, "state": "untested"}

    def test_dead_model_includes_reason_failures_until(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "Provider returned error", now=T0)
        s = ledger.status(MODEL, now=T0 + timedelta(minutes=2))
        assert s["state"] == "dead"
        assert s["reason"] == "Provider returned error"
        assert s["failures"] == 1
        assert "until" in s

    def test_live_after_success_includes_last_success(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        t_success = T0 + timedelta(minutes=10)
        ledger.mark_success(MODEL, now=t_success)
        s = ledger.status(MODEL, now=T0 + timedelta(minutes=11))
        assert s["state"] == "live"
        assert "last_success" in s

    def test_expired_dead_window_returns_live(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable(MODEL, "err", now=T0)
        # After the 5-min window expires
        s = ledger.status(MODEL, now=T0 + timedelta(minutes=6))
        assert s["state"] == "live"

    def test_reason_truncated_to_200_chars(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        long_reason = "x" * 300
        ledger.mark_unavailable(MODEL, long_reason, now=T0)
        s = ledger.status(MODEL, now=T0)
        assert len(s["reason"]) == 200


# ---------------------------------------------------------------------------
# all_status
# ---------------------------------------------------------------------------


class TestAllStatus:
    def test_returns_one_entry_per_model(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable("model/a:free", "err", now=T0)
        results = ledger.all_status(["model/a:free", "model/b:free"], now=T0)
        assert len(results) == 2

    def test_correct_states_per_model(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        ledger.mark_unavailable("model/a:free", "err", now=T0)
        results = ledger.all_status(["model/a:free", "model/b:free"], now=T0)
        assert results[0]["model"] == "model/a:free"
        assert results[0]["state"] == "dead"
        assert results[1]["model"] == "model/b:free"
        assert results[1]["state"] == "untested"

    def test_empty_model_list_returns_empty(self, tmp_path: Path) -> None:
        assert _ledger(tmp_path).all_status([], now=T0) == []
