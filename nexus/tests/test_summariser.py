"""Tests for cascade/summariser.py — Haiku summariser routing."""

from unittest.mock import AsyncMock, MagicMock

from orchestrator.cascade.summariser import SUMMARISER_SYSTEM_PROMPT, Summariser
from orchestrator.types import NexusMessage, NexusRequest, NexusResponse, StyleProfile

_HAIKU_BODY = (
    "## Summary\nTest summary.\n\n"
    "## Open Questions\n- None.\n\n"
    "## Last User Intent\nUser asked about X.\n\n"
    "## Recommended Continuation Style\nBe concise.\n\n"
    "## Full History Path\n[[test-thread]]"
)


def _make_adapter(body: str = _HAIKU_BODY) -> MagicMock:
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=NexusResponse(
            content=body,
            model_id="claude-haiku-4-5",
            auth_mode="subscription",
            estimated_tokens=120,
            duration_ms=800,
            finish_reason="stop",
        )
    )
    return adapter


def _make_summariser(body: str = _HAIKU_BODY) -> tuple[Summariser, MagicMock]:
    adapter = _make_adapter(body)
    return Summariser(adapter), adapter


_TRANSCRIPT = [
    NexusMessage(role="user", content="Hello"),
    NexusMessage(role="assistant", content="Hi there"),
    NexusMessage(role="user", content="What is the cascade?"),
]


class TestSummariserReturnsContent:
    async def test_returns_adapter_content(self) -> None:
        s, _ = _make_summariser()
        result = await s.summarise_for_handover(_TRANSCRIPT, StyleProfile(), "[[link]]")
        assert result == _HAIKU_BODY

    async def test_empty_transcript_still_calls_adapter(self) -> None:
        s, adapter = _make_summariser("## Summary\nEmpty.")
        result = await s.summarise_for_handover([], StyleProfile(), "[[link]]")
        assert result == "## Summary\nEmpty."
        adapter.complete.assert_awaited_once()


class TestSummariserRouting:
    async def test_selection_hint_is_summariser(self) -> None:
        s, adapter = _make_summariser()
        await s.summarise_for_handover(_TRANSCRIPT, StyleProfile(), "[[link]]")
        request: NexusRequest = adapter.complete.call_args.args[0]
        assert request.selection_hint == "summariser"

    async def test_system_prompt_matches_constant(self) -> None:
        s, adapter = _make_summariser()
        await s.summarise_for_handover(_TRANSCRIPT, StyleProfile(), "[[link]]")
        request: NexusRequest = adapter.complete.call_args.args[0]
        system_msgs = [m for m in request.messages if m.role == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0].content == SUMMARISER_SYSTEM_PROMPT

    async def test_wiki_link_in_user_message(self) -> None:
        s, adapter = _make_summariser()
        await s.summarise_for_handover(_TRANSCRIPT, StyleProfile(), "[[my-thread]]")
        request: NexusRequest = adapter.complete.call_args.args[0]
        user_msgs = [m for m in request.messages if m.role == "user"]
        assert any("[[my-thread]]" in m.content for m in user_msgs)

    async def test_stream_is_false(self) -> None:
        s, adapter = _make_summariser()
        await s.summarise_for_handover(_TRANSCRIPT, StyleProfile(), "[[link]]")
        request: NexusRequest = adapter.complete.call_args.args[0]
        assert request.stream is False


class TestBuildPrompt:
    def test_includes_transcript_content(self) -> None:
        s, _ = _make_summariser()
        prompt = s._build_prompt(_TRANSCRIPT, StyleProfile(), "[[link]]")
        assert "Hello" in prompt
        assert "What is the cascade?" in prompt

    def test_includes_style_profile(self) -> None:
        s, _ = _make_summariser()
        style = StyleProfile(format="structured", verbosity="concise", posture="peer")
        prompt = s._build_prompt([], style, "[[link]]")
        assert "structured" in prompt
        assert "concise" in prompt
        assert "peer" in prompt

    def test_includes_wiki_link(self) -> None:
        s, _ = _make_summariser()
        prompt = s._build_prompt([], StyleProfile(), "[[specific-link]]")
        assert "[[specific-link]]" in prompt

    def test_system_messages_labeled(self) -> None:
        s, _ = _make_summariser()
        transcript = [NexusMessage(role="system", content="You are helpful.")]
        prompt = s._build_prompt(transcript, StyleProfile(), "[[link]]")
        assert "SYSTEM" in prompt
        assert "You are helpful." in prompt

    def test_user_messages_labeled_human(self) -> None:
        s, _ = _make_summariser()
        transcript = [NexusMessage(role="user", content="User question")]
        prompt = s._build_prompt(transcript, StyleProfile(), "[[link]]")
        assert "Human:" in prompt

    def test_assistant_messages_labeled(self) -> None:
        s, _ = _make_summariser()
        transcript = [NexusMessage(role="assistant", content="Model reply")]
        prompt = s._build_prompt(transcript, StyleProfile(), "[[link]]")
        assert "Assistant:" in prompt
