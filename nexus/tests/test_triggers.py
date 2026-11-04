"""Tests for cascade trigger taxonomy classifier and LatencyTracker."""

from orchestrator.adapters.base import (
    AdapterContextOverflow,
    AdapterError,
    AdapterQuotaExhausted,
    AdapterRateLimit,
    AdapterRefusal,
    AdapterTimeout,
    AdapterUnavailable,
)
from orchestrator.cascade.triggers import (
    LatencyTracker,
    TriggerClass,
    _seconds_until_next_openrouter_reset,
    classify_error,
    manual_trigger,
)

# ---------------------------------------------------------------------------
# classify_error — Anthropic Pro quota
# ---------------------------------------------------------------------------


class TestClassifyAnthropicQuota:
    def test_trigger_class(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("Pro quota hit"), "anthropic")
        assert outcome.trigger == TriggerClass.PRO_QUOTA_EXHAUSTED

    def test_no_retry(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("quota"), "anthropic")
        assert not outcome.retryable

    def test_failover(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("quota"), "anthropic")
        assert outcome.failover

    def test_cooldown_approx_7_days(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("quota"), "anthropic")
        assert outcome.cooldown_seconds == 7 * 24 * 3600

    def test_no_cross_rung_cooldown(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("quota"), "anthropic")
        assert not outcome.cross_rung_cooldown


# ---------------------------------------------------------------------------
# classify_error — OpenRouter quota
# ---------------------------------------------------------------------------


class TestClassifyOpenRouterQuota:
    def test_trigger_class(self) -> None:
        outcome = classify_error(
            AdapterQuotaExhausted("daily limit", cooldown_key="openrouter_shared"),
            "openrouter",
        )
        assert outcome.trigger == TriggerClass.OPENROUTER_QUOTA

    def test_no_retry(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("daily limit"), "openrouter")
        assert not outcome.retryable

    def test_failover(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("daily limit"), "openrouter")
        assert outcome.failover

    def test_cross_rung_cooldown(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("daily limit"), "openrouter")
        assert outcome.cross_rung_cooldown

    def test_cooldown_within_one_day(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("daily limit"), "openrouter")
        assert outcome.cooldown_seconds is not None
        assert 0 < outcome.cooldown_seconds <= 86400


# ---------------------------------------------------------------------------
# classify_error — unknown adapter with AdapterQuotaExhausted falls to HARD_FAIL
# ---------------------------------------------------------------------------


class TestClassifyGoogleQuota:
    def test_falls_to_hard_fail(self) -> None:
        outcome = classify_error(AdapterQuotaExhausted("google limit"), "google")
        assert outcome.trigger == TriggerClass.HARD_FAIL


# ---------------------------------------------------------------------------
# classify_error — universal mappings
# ---------------------------------------------------------------------------


class TestClassifyUniversal:
    def test_unavailable_hard_fail(self) -> None:
        outcome = classify_error(AdapterUnavailable("down"), "anthropic")
        assert outcome.trigger == TriggerClass.HARD_FAIL
        assert not outcome.retryable
        assert outcome.failover

    def test_rate_limit_soft_degrade(self) -> None:
        outcome = classify_error(AdapterRateLimit("429"), "openrouter")
        assert outcome.trigger == TriggerClass.SOFT_DEGRADE
        assert outcome.retryable
        assert not outcome.failover
        assert outcome.cooldown_seconds == 30

    def test_timeout_soft_degrade(self) -> None:
        outcome = classify_error(AdapterTimeout("timed out"), "ollama")
        assert outcome.trigger == TriggerClass.SOFT_DEGRADE
        assert not outcome.retryable
        assert outcome.failover

    def test_context_overflow(self) -> None:
        outcome = classify_error(AdapterContextOverflow("too long"), "openrouter")
        assert outcome.trigger == TriggerClass.CONTEXT_LIMIT
        assert not outcome.retryable
        assert outcome.failover

    def test_refusal_retryable_and_failover(self) -> None:
        outcome = classify_error(AdapterRefusal("content policy"), "anthropic")
        assert outcome.trigger == TriggerClass.EXPLICIT_REFUSAL
        assert outcome.retryable
        assert outcome.failover

    def test_unknown_adapter_error_hard_fail(self) -> None:
        class _UnknownError(AdapterError):
            pass

        outcome = classify_error(_UnknownError("something weird"), "anthropic")
        assert outcome.trigger == TriggerClass.HARD_FAIL
        assert not outcome.retryable
        assert outcome.failover


# ---------------------------------------------------------------------------
# manual_trigger
# ---------------------------------------------------------------------------


class TestManualTrigger:
    def test_class(self) -> None:
        assert manual_trigger().trigger == TriggerClass.MANUAL

    def test_failover(self) -> None:
        assert manual_trigger().failover

    def test_not_retryable(self) -> None:
        assert not manual_trigger().retryable


# ---------------------------------------------------------------------------
# _seconds_until_next_openrouter_reset
# ---------------------------------------------------------------------------


class TestOpenRouterResetTimer:
    def test_positive_and_within_one_day(self) -> None:
        secs = _seconds_until_next_openrouter_reset()
        assert 0 < secs <= 86400


# ---------------------------------------------------------------------------
# LatencyTracker
# ---------------------------------------------------------------------------


class TestLatencyTracker:
    def _stable(self, window: int = 20, baseline_ms: float = 500.0) -> LatencyTracker:
        """Return a tracker with a full window of stable baseline samples."""
        t = LatencyTracker(window_size=window)
        for _ in range(window):
            t.record("adapter", baseline_ms)
        return t

    def test_no_samples_never_degrading(self) -> None:
        t = LatencyTracker()
        assert not t.is_degrading("adapter", 9999.0)

    def test_insufficient_samples_never_degrading(self) -> None:
        t = LatencyTracker(window_size=20)
        for _ in range(19):
            t.record("adapter", 1000.0)
        assert not t.is_degrading("adapter", 9999.0)

    def test_one_slow_sample_not_degrading(self) -> None:
        # First slow observation doesn't cross the 2-consecutive threshold
        t = self._stable(baseline_ms=500.0)
        assert not t.is_degrading("adapter", 5000.0)

    def test_two_consecutive_slow_samples_degrading(self) -> None:
        t = self._stable(baseline_ms=500.0)
        t.is_degrading("adapter", 5000.0)  # first slow — not yet
        assert t.is_degrading("adapter", 5000.0)  # second slow — degrading

    def test_fast_sample_resets_counter(self) -> None:
        t = self._stable(baseline_ms=500.0)
        t.is_degrading("adapter", 5000.0)  # first slow
        t.is_degrading("adapter", 400.0)   # fast — resets
        assert not t.is_degrading("adapter", 5000.0)  # first slow again, not yet 2

    def test_different_adapters_tracked_independently(self) -> None:
        t = LatencyTracker(window_size=5)
        for _ in range(5):
            t.record("a", 500.0)
        t.is_degrading("a", 5000.0)
        t.is_degrading("a", 5000.0)
        # adapter "b" has no samples — insufficient window, never degrades
        assert not t.is_degrading("b", 9999.0)
