"""Tests for orchestrator/heuristics/save_worthiness.py"""

from orchestrator.heuristics.save_worthiness import (
    _is_pure_greeting,
    _is_short_and_plain,
    is_worth_saving,
)

_SHORT_REPLY = "Hi there!"  # < 20 chars — trivial
_SUBSTANTIVE_REPLY = (
    "Sure — a Markov chain is a sequence of states where the next state "
    "depends only on the current one, not the full history."
)  # >= 20 chars — substantive


# ---------------------------------------------------------------------------
# The four explicit boundary cases from the ticket
# ---------------------------------------------------------------------------


class TestBoundaryCases:
    def test_hi_skips(self) -> None:
        assert is_worth_saving("hi", _SHORT_REPLY) is False

    def test_hi_do_you_know_what_markov_chains_are_saves(self) -> None:
        assert is_worth_saving(
            "hi, do you know what markov chains are", _SUBSTANTIVE_REPLY
        ) is True

    def test_ok_skips(self) -> None:
        assert is_worth_saving("ok", _SHORT_REPLY) is False

    def test_ok_ill_use_flask_then_saves(self) -> None:
        assert is_worth_saving(
            "ok I'll use flask then",
            "Good call — Flask's simplicity fits a small service like this well.",
        ) is True


# ---------------------------------------------------------------------------
# Conservatism: a substantive user message always saves, regardless of
# how short the reply is.
# ---------------------------------------------------------------------------


class TestBiasTowardSaving:
    def test_real_question_with_short_answer_still_saves(self) -> None:
        assert is_worth_saving("why does this raise a KeyError?", "Because.") is True

    def test_short_message_with_question_mark_saves(self) -> None:
        """<15 chars but has '?' — the length rule's AND fails, so it's not
        trivial-by-length; not a pure greeting either — must save."""
        assert is_worth_saving("why?", "Because.") is True

    def test_short_message_with_code_fence_saves(self) -> None:
        """<15 chars but contains a code fence — length rule's AND fails."""
        assert is_worth_saving("```py\nx=1\n```", "Noted.") is True

    def test_long_reply_to_a_greeting_saves(self) -> None:
        """User message is trivial, but the assistant gave a substantial
        reply anyway — bias toward saving means we keep it."""
        assert is_worth_saving("hi", _SUBSTANTIVE_REPLY) is True


# ---------------------------------------------------------------------------
# Pure greeting/ack matching — whole-message match, not substring
# ---------------------------------------------------------------------------


class TestPureGreetingMatching:
    def test_matches_bare_words(self) -> None:
        for word in ("hi", "hey", "hello", "thanks", "ok", "cool"):
            assert _is_pure_greeting(word) is True, word

    def test_matches_case_insensitively(self) -> None:
        assert _is_pure_greeting("Thanks") is True
        assert _is_pure_greeting("OK") is True
        assert _is_pure_greeting("COOL") is True

    def test_matches_with_trailing_punctuation(self) -> None:
        assert _is_pure_greeting("thanks!") is True
        assert _is_pure_greeting("ok.") is True
        assert _is_pure_greeting("cool,") is True

    def test_matches_with_surrounding_whitespace(self) -> None:
        assert _is_pure_greeting("  hi  ") is True

    def test_does_not_match_as_substring_of_a_longer_message(self) -> None:
        assert _is_pure_greeting("ok I'll use flask then") is False
        assert _is_pure_greeting("hi, do you know what markov chains are") is False
        assert _is_pure_greeting("thanks for the help with the migration") is False

    def test_does_not_match_unrelated_words(self) -> None:
        assert _is_pure_greeting("flask") is False
        assert _is_pure_greeting("why does this fail") is False


# ---------------------------------------------------------------------------
# Short-and-plain length rule
# ---------------------------------------------------------------------------


class TestShortAndPlain:
    def test_short_plain_message_is_short_and_plain(self) -> None:
        assert _is_short_and_plain("sounds good") is True

    def test_long_message_is_not(self) -> None:
        assert _is_short_and_plain("a" * 20) is False

    def test_short_message_with_question_mark_is_not(self) -> None:
        assert _is_short_and_plain("why?") is False

    def test_short_message_with_code_fence_is_not(self) -> None:
        assert _is_short_and_plain("```\nx\n```") is False

    def test_boundary_at_15_chars(self) -> None:
        assert _is_short_and_plain("a" * 14) is True
        assert _is_short_and_plain("a" * 15) is False


# ---------------------------------------------------------------------------
# Assistant-reply triviality threshold
# ---------------------------------------------------------------------------


class TestAssistantReplyThreshold:
    def test_19_char_reply_is_trivial(self) -> None:
        assert is_worth_saving("hi", "a" * 19) is False

    def test_20_char_reply_is_not_trivial(self) -> None:
        assert is_worth_saving("hi", "a" * 20) is True

    def test_reply_whitespace_is_stripped_before_measuring(self) -> None:
        assert is_worth_saving("hi", "   hi!   ") is False
