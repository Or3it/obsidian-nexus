"""Tests for orchestrator/heuristics/opus.py"""

from orchestrator.heuristics.opus import _DEFAULT_KEYWORDS, OpusHeuristic
from orchestrator.types import OpusHeuristicConfig


def _heuristic(
    enabled: bool = True,
    min_chars: int = 500,
    keywords: list[str] | None = None,
) -> OpusHeuristic:
    return OpusHeuristic(
        OpusHeuristicConfig(enabled=enabled, min_chars=min_chars, keywords=keywords)
    )


def _long(suffix: str = "") -> str:
    """Return a string ≥ 500 chars."""
    base = "a " * 260  # 520 chars
    return base + suffix


class TestDisabled:
    def test_returns_false_when_disabled(self) -> None:
        h = _heuristic(enabled=False)
        assert h.should_escalate(_long("design tradeoff")) is False

    def test_disabled_regardless_of_message_length_and_keywords(self) -> None:
        h = _heuristic(enabled=False)
        msg = _long("design architecture review analyze")
        assert h.should_escalate(msg) is False


class TestLengthGating:
    def test_short_message_never_escalates(self) -> None:
        h = _heuristic(enabled=True, min_chars=500)
        assert h.should_escalate("design tradeoff") is False

    def test_exact_min_chars_escalates_with_keyword(self) -> None:
        h = _heuristic(enabled=True, min_chars=10, keywords=["design"])
        assert h.should_escalate("x" * 5 + " design") is True  # 12 chars

    def test_one_char_below_threshold_does_not_escalate(self) -> None:
        h = _heuristic(enabled=True, min_chars=500)
        msg = "design " + "x" * 492  # 499 chars, has keyword
        assert len(msg) == 499
        assert h.should_escalate(msg) is False

    def test_exactly_threshold_chars_escalates(self) -> None:
        h = _heuristic(enabled=True, min_chars=500)
        # 500 chars exactly, contains "design"
        filler = "x" * (500 - len("design "))
        msg = "design " + filler
        assert len(msg) == 500
        assert h.should_escalate(msg) is True


class TestKeywordMatching:
    def test_manual_test_case_escalates(self) -> None:
        """The canonical manual test: >500 chars with 'design' and 'tradeoff'."""
        h = _heuristic(enabled=True)
        msg = _long("design tradeoff")
        assert len(msg) > 500
        assert h.should_escalate(msg) is True

    def test_no_keyword_does_not_escalate(self) -> None:
        h = _heuristic(enabled=True)
        msg = _long("please tell me about the weather")
        assert h.should_escalate(msg) is False

    def test_single_matching_keyword_sufficient(self) -> None:
        for kw in _DEFAULT_KEYWORDS:
            h = _heuristic(enabled=True, keywords=[kw])
            assert h.should_escalate(_long(kw)) is True, f"keyword {kw!r} did not fire"

    def test_keyword_matching_is_case_insensitive(self) -> None:
        h = _heuristic(enabled=True, keywords=["design"])
        assert h.should_escalate(_long("DESIGN")) is True
        assert h.should_escalate(_long("Design")) is True
        assert h.should_escalate(_long("DeSiGn")) is True

    def test_keyword_as_substring(self) -> None:
        """'redesign' contains 'design' — substring match is intentional."""
        h = _heuristic(enabled=True, keywords=["design"])
        assert h.should_escalate(_long("please redesign this")) is True

    def test_custom_keywords_override_defaults(self) -> None:
        h = _heuristic(enabled=True, keywords=["unicorn"])
        assert h.should_escalate(_long("design tradeoff")) is False
        assert h.should_escalate(_long("unicorn")) is True

    def test_none_keywords_uses_defaults(self) -> None:
        h = _heuristic(enabled=True, keywords=None)
        assert h.should_escalate(_long("design")) is True
        assert h.should_escalate(_long("tradeoff")) is True

    def test_empty_keyword_list_never_escalates(self) -> None:
        h = _heuristic(enabled=True, keywords=[])
        assert h.should_escalate(_long("design tradeoff architecture")) is False


class TestConfig:
    def test_default_config_disabled(self) -> None:
        """Default OpusHeuristicConfig has enabled=False."""
        cfg = OpusHeuristicConfig()
        assert cfg.enabled is False

    def test_default_min_chars_is_500(self) -> None:
        cfg = OpusHeuristicConfig()
        assert cfg.min_chars == 500

    def test_default_keywords_is_none(self) -> None:
        cfg = OpusHeuristicConfig()
        assert cfg.keywords is None
