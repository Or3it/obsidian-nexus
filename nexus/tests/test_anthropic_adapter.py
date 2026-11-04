"""Tests for orchestrator/adapters/anthropic.py — all subprocess calls are mocked."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.adapters.anthropic import AnthropicAdapter
from orchestrator.adapters.base import AdapterTimeout, AdapterUnavailable
from orchestrator.heuristics.opus import OpusHeuristic
from orchestrator.prompts import NEXUS_STYLE, RESEARCH_PREAMBLE
from orchestrator.types import NexusMessage, NexusRequest, OpusHeuristicConfig, StyleProfile

FIXTURES = Path(__file__).parent / "fixtures"


def _make_request(*contents: str) -> NexusRequest:
    messages = [NexusMessage(role="user", content=c) for c in contents]
    return NexusRequest(
        thread_id="test-thread",
        messages=messages,
        style_profile=StyleProfile(),
    )


def _make_mock_proc(
    stdout_lines: list[bytes],
    returncode: int = 0,
    stderr: bytes = b"",
) -> MagicMock:
    """Build a mock asyncio.Process that yields the given stdout lines then EOF."""
    proc = MagicMock()
    proc.returncode = None

    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    # readline() pops lines in order; returns b"" (EOF) after the last one
    lines_iter = list(stdout_lines) + [b""]
    call_index = [0]

    async def readline() -> bytes:
        idx = call_index[0]
        call_index[0] += 1
        return lines_iter[idx] if idx < len(lines_iter) else b""

    proc.stdout = MagicMock()
    proc.stdout.readline = readline

    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)

    async def wait() -> None:
        proc.returncode = returncode

    proc.wait = wait
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    return proc


def _fixture_lines() -> list[bytes]:
    """Return non-empty lines from the claude_code_stdout.txt fixture."""
    raw = (FIXTURES / "claude_code_stdout.txt").read_bytes().splitlines()
    return [ln for ln in raw if ln]


# ──────────────────────────────────────────────────────────────────────────────


class TestCompleteStream:
    async def test_yields_tokens_from_fixture(self) -> None:
        """complete_stream yields one text token ('ping') then a final token."""
        proc = _make_mock_proc(_fixture_lines())
        adapter = AnthropicAdapter()
        request = _make_request("pong")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            tokens = [t async for t in adapter.complete_stream(request)]

        text_tokens = [t for t in tokens if not t.is_final]
        final_tokens = [t for t in tokens if t.is_final]
        assert len(text_tokens) == 1
        assert text_tokens[0].text == "ping"
        assert len(final_tokens) == 1
        assert final_tokens[0].text == ""


class TestComplete:
    async def test_content_equals_concatenated_stream_text(self) -> None:
        """complete() joins all non-final token texts into NexusResponse.content."""
        proc = _make_mock_proc(_fixture_lines())
        adapter = AnthropicAdapter()
        request = _make_request("pong")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            response = await adapter.complete(request)

        assert response.content == "ping"
        assert response.model_id == "claude-sonnet-4-6"
        assert response.auth_mode == "subscription"
        assert response.finish_reason == "stop"

    async def test_empty_stdout_yields_zero_tokens(self) -> None:
        """Pure EOF (no JSON lines) → empty content, finish_reason='stop', no crash."""
        proc = _make_mock_proc([])  # EOF immediately
        adapter = AnthropicAdapter()
        request = _make_request("hello")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            response = await adapter.complete(request)

        assert response.content == ""
        assert response.finish_reason == "stop"


class TestTimeoutAndErrors:
    async def test_subprocess_timeout_raises_adapter_timeout(self) -> None:
        """A stalled readline raises AdapterTimeout after the configured timeout."""
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.terminate = MagicMock()
        proc.kill = MagicMock()

        async def hanging_readline() -> bytes:
            await asyncio.sleep(10)  # far longer than the 0.05 s adapter timeout
            return b""

        proc.stdout = MagicMock()
        proc.stdout.readline = hanging_readline

        async def wait() -> None:
            proc.returncode = 0

        proc.wait = wait

        adapter = AnthropicAdapter(timeout_seconds=1)
        request = _make_request("hi")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(AdapterTimeout, match="timed out"):
                async for _ in adapter.complete_stream(request):
                    pass

    async def test_nonzero_exit_raises_adapter_unavailable_with_stderr(self) -> None:
        """Non-zero exit code raises AdapterUnavailable including stderr content."""
        proc = _make_mock_proc(
            stdout_lines=[],
            returncode=1,
            stderr=b"fatal: something went wrong",
        )
        adapter = AnthropicAdapter()
        request = _make_request("hi")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(AdapterUnavailable, match="something went wrong"):
                async for _ in adapter.complete_stream(request):
                    pass


class TestCommandBuilding:
    def test_use_ruflo_false_uses_claude_base(self) -> None:
        """use_ruflo=False produces a command whose first element is 'claude'."""
        adapter = AnthropicAdapter(use_ruflo=False)
        cmd = adapter._build_command(_make_request("hi"))
        assert cmd[0] == "claude"
        assert "npx" not in cmd

    def test_nexus_voice_appended_on_every_call(self) -> None:
        """--append-system-prompt carries NEXUS_STYLE on all subprocess calls."""
        adapter = AnthropicAdapter(use_ruflo=False)
        cmd = adapter._build_command(_make_request("hi"))
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == NEXUS_STYLE

    def test_research_mode_concatenates_preamble_after_voice(self) -> None:
        adapter = AnthropicAdapter(use_ruflo=False)
        request = _make_request("what changed this week?").model_copy(
            update={"mode": "research"}
        )
        cmd = adapter._build_command(request)
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == NEXUS_STYLE + "\n\n" + RESEARCH_PREAMBLE

    def test_non_research_modes_omit_preamble(self) -> None:
        adapter = AnthropicAdapter(use_ruflo=False)
        for mode in ("auto", "light", "hard"):
            request = _make_request("hi").model_copy(update={"mode": mode})
            cmd = adapter._build_command(request)
            idx = cmd.index("--append-system-prompt")
            assert RESEARCH_PREAMBLE not in cmd[idx + 1], f"mode={mode}"

    async def test_use_ruflo_true_raises_adapter_unavailable(self) -> None:
        """use_ruflo=True raises AdapterUnavailable at iteration start; no subprocess."""
        adapter = AnthropicAdapter(use_ruflo=True)
        request = _make_request("hi")

        with pytest.raises(AdapterUnavailable, match="use_ruflo=True is not functional"):
            async for _ in adapter.complete_stream(request):
                pass


class TestTokenUtils:
    def test_count_tokens_approximate(self) -> None:
        """count_tokens returns max(1, int(len(text) / 3.8))."""
        adapter = AnthropicAdapter()
        text = "hello world"
        assert adapter.count_tokens(text) == max(1, int(len(text) / 3.8))

    def test_available_context_returns_configured_window(self) -> None:
        """available_context returns the context_window passed at construction."""
        adapter = AnthropicAdapter(context_window=50_000)
        assert adapter.available_context() == 50_000


class TestModelRouting:
    """Phase 2a: _resolve_model and _build_command pick the right model."""

    def _make_request_with_hint(
        self, content: str = "hi", hint: str | None = None
    ) -> NexusRequest:
        return NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content=content)],
            style_profile=StyleProfile(),
            selection_hint=hint,
        )

    def test_default_request_routes_to_sonnet(self) -> None:
        """No hint and no #opus tag → default Sonnet model in command."""
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(self._make_request_with_hint())
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"

    def test_selection_hint_summariser_routes_to_haiku(self) -> None:
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(self._make_request_with_hint(hint="summariser"))
        assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5"

    def test_selection_hint_premium_routes_to_opus(self) -> None:
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(self._make_request_with_hint(hint="premium"))
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"

    def test_selection_hint_pin_uses_exact_model_string(self) -> None:
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(
            self._make_request_with_hint(hint="pin:claude-opus-4-7")
        )
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"

    def test_selection_hint_pin_haiku_uses_haiku(self) -> None:
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(
            self._make_request_with_hint(hint="pin:claude-haiku-4-5")
        )
        assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5"

    def test_opus_tag_in_last_user_message_routes_to_opus(self) -> None:
        """#opus in last user message → Opus, regardless of selection_hint."""
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(
            self._make_request_with_hint("Say hello. #opus")
        )
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"

    def test_opus_tag_uppercase_routes_to_opus(self) -> None:
        """#OPUS (uppercase) is treated the same as #opus."""
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(
            self._make_request_with_hint("Say hello. #OPUS")
        )
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"

    def test_opus_tag_in_non_last_message_does_not_trigger_routing(self) -> None:
        """#opus in a mid-conversation message does NOT route to Opus."""
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="user", content="First message #opus"),
                NexusMessage(role="assistant", content="Response"),
                NexusMessage(role="user", content="Second message (no tag)"),
            ],
            style_profile=StyleProfile(),
        )
        cmd = adapter._build_command(request)
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"

    def test_opus_tag_takes_priority_over_summariser_hint(self) -> None:
        """#opus in message overrides selection_hint='summariser'."""
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(
            self._make_request_with_hint("hi #opus", hint="summariser")
        )
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"

    def test_unknown_hint_falls_back_to_default(self) -> None:
        """Unrecognized hint (not 'summariser'/'premium'/'pin:*') → default model."""
        adapter = AnthropicAdapter()
        cmd = adapter._build_command(
            self._make_request_with_hint(hint="some-unknown-hint")
        )
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"

    def test_custom_model_strings_respected(self) -> None:
        """Custom model strings passed to constructor are used in routing."""
        adapter = AnthropicAdapter(
            default_model="claude-sonnet-4-6",
            premium_model="claude-opus-4-7",
            summariser_model="claude-haiku-4-5-20251001",
        )
        cmd = adapter._build_command(
            self._make_request_with_hint(hint="summariser")
        )
        assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Opus heuristic routing (P2b-T06)
# ---------------------------------------------------------------------------


def _heuristic(
    enabled: bool = True,
    min_chars: int = 500,
    keywords: list[str] | None = None,
) -> OpusHeuristic:
    cfg = OpusHeuristicConfig(enabled=enabled, min_chars=min_chars, keywords=keywords)
    return OpusHeuristic(cfg)


def _long_message(suffix: str = "") -> str:
    return "x " * 260 + suffix  # 520+ chars


class TestOpusHeuristicRouting:
    """_resolve_model step 5: auto-escalation heuristic."""

    def _adapter_with_heuristic(self, **heuristic_kwargs: Any) -> AnthropicAdapter:
        return AnthropicAdapter(opus_heuristic=_heuristic(**heuristic_kwargs))

    def test_no_heuristic_defaults_to_sonnet(self) -> None:
        adapter = AnthropicAdapter()  # no heuristic
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content=_long_message("design tradeoff"))],
            style_profile=StyleProfile(),
        )
        assert adapter._resolve_model(request) == "claude-sonnet-4-6"

    def test_heuristic_disabled_defaults_to_sonnet(self) -> None:
        adapter = self._adapter_with_heuristic(enabled=False)
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content=_long_message("design tradeoff"))],
            style_profile=StyleProfile(),
        )
        assert adapter._resolve_model(request) == "claude-sonnet-4-6"

    def test_long_message_with_keyword_escalates_to_opus(self) -> None:
        """The canonical manual test case."""
        adapter = self._adapter_with_heuristic(keywords=["design", "tradeoff"])
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content=_long_message("design tradeoff"))],
            style_profile=StyleProfile(),
        )
        assert adapter._resolve_model(request) == "claude-opus-4-7"

    def test_short_message_does_not_escalate(self) -> None:
        adapter = self._adapter_with_heuristic(keywords=["design"])
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="design tradeoff")],
            style_profile=StyleProfile(),
        )
        assert adapter._resolve_model(request) == "claude-sonnet-4-6"

    def test_opus_tag_takes_priority_over_heuristic(self) -> None:
        adapter = self._adapter_with_heuristic()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="#opus short")],
            style_profile=StyleProfile(),
        )
        assert adapter._resolve_model(request) == "claude-opus-4-7"

    def test_summariser_hint_bypasses_heuristic(self) -> None:
        adapter = self._adapter_with_heuristic()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content=_long_message("design tradeoff"))],
            style_profile=StyleProfile(),
            selection_hint="summariser",
        )
        assert adapter._resolve_model(request) == "claude-haiku-4-5"

    def test_pin_hint_bypasses_heuristic(self) -> None:
        adapter = self._adapter_with_heuristic()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content=_long_message("design tradeoff"))],
            style_profile=StyleProfile(),
            selection_hint="pin:claude-sonnet-4-6",
        )
        assert adapter._resolve_model(request) == "claude-sonnet-4-6"

    def test_no_user_message_does_not_escalate(self) -> None:
        """If there is no user message, heuristic cannot fire."""
        adapter = self._adapter_with_heuristic()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="system", content=_long_message("design"))],
            style_profile=StyleProfile(),
        )
        assert adapter._resolve_model(request) == "claude-sonnet-4-6"

    def test_heuristic_checks_last_user_message_only(self) -> None:
        """Keyword in an older message but not the latest one should not escalate."""
        adapter = self._adapter_with_heuristic(keywords=["design"])
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="user", content=_long_message("design architecture")),
                NexusMessage(role="assistant", content="ok"),
                NexusMessage(role="user", content="thanks"),
            ],
            style_profile=StyleProfile(),
        )
        assert adapter._resolve_model(request) == "claude-sonnet-4-6"


class TestOpusTagStripping:
    """Phase 2a: #opus tag must be stripped from prompt text sent to subprocess."""

    async def test_opus_tag_stripped_from_prompt_sent_to_subprocess(self) -> None:
        """Prompt written to stdin must not contain #opus."""
        proc = _make_mock_proc(_fixture_lines())
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="Say hello. #opus")],
            style_profile=StyleProfile(),
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            async for _ in adapter.complete_stream(request):
                pass

        written: str = proc.stdin.write.call_args[0][0].decode("utf-8")
        assert "#opus" not in written.lower()
        assert "Say hello." in written

    async def test_opus_tag_uppercase_stripped(self) -> None:
        """#OPUS (uppercase) is also stripped."""
        proc = _make_mock_proc(_fixture_lines())
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="Hello #OPUS world")],
            style_profile=StyleProfile(),
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            async for _ in adapter.complete_stream(request):
                pass

        written: str = proc.stdin.write.call_args[0][0].decode("utf-8")
        assert "#opus" not in written.lower()
        assert "Hello" in written
        assert "world" in written

    async def test_opus_tag_in_non_last_message_preserved(self) -> None:
        """#opus in a non-last message is NOT stripped (not a routing signal there)."""
        proc = _make_mock_proc(_fixture_lines())
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="user", content="First #opus message"),
                NexusMessage(role="assistant", content="ok"),
                NexusMessage(role="user", content="Follow-up"),
            ],
            style_profile=StyleProfile(),
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            async for _ in adapter.complete_stream(request):
                pass

        written: str = proc.stdin.write.call_args[0][0].decode("utf-8")
        # The #opus in the first (non-last) message is kept intact
        assert "#opus" in written.lower()

    def test_build_prompt_text_strips_only_last_user(self) -> None:
        """_build_prompt_text strips #opus from the last user message only."""
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="user", content="Earlier #opus"),
                NexusMessage(role="assistant", content="ok"),
                NexusMessage(role="user", content="Final #opus"),
            ],
            style_profile=StyleProfile(),
        )
        prompt = adapter._build_prompt_text(request)
        # Earlier message keeps tag; last message has it stripped
        assert "Earlier #opus" in prompt
        assert "Final #opus" not in prompt
        assert "Final" in prompt


class TestModelAwareTokensAndContext:
    """Phase 2a: count_tokens and available_context differ by configured model."""

    def test_count_tokens_sonnet_uses_3_8_ratio(self) -> None:
        adapter = AnthropicAdapter(default_model="claude-sonnet-4-6")
        text = "hello world"
        assert adapter.count_tokens(text) == max(1, int(len(text) / 3.8))

    def test_count_tokens_opus_uses_3_6_ratio(self) -> None:
        """Opus 4.7 tokenizer is more verbose (3.6 chars/token)."""
        adapter = AnthropicAdapter(default_model="claude-opus-4-7")
        text = "hello world"
        assert adapter.count_tokens(text) == max(1, int(len(text) / 3.6))

    def test_count_tokens_haiku_uses_3_9_ratio(self) -> None:
        adapter = AnthropicAdapter(default_model="claude-haiku-4-5")
        text = "hello world"
        assert adapter.count_tokens(text) == max(1, int(len(text) / 3.9))

    def test_count_tokens_opus_less_than_haiku_for_same_text(self) -> None:
        """Opus has smaller chars/token ratio → reports more tokens for same text."""
        text = "a" * 100
        opus = AnthropicAdapter(default_model="claude-opus-4-7").count_tokens(text)
        haiku = AnthropicAdapter(default_model="claude-haiku-4-5").count_tokens(text)
        assert opus > haiku

    def test_available_context_sonnet(self) -> None:
        assert AnthropicAdapter(default_model="claude-sonnet-4-6").available_context() == 200_000

    def test_available_context_opus(self) -> None:
        assert AnthropicAdapter(default_model="claude-opus-4-7").available_context() == 1_000_000

    def test_available_context_haiku(self) -> None:
        assert AnthropicAdapter(default_model="claude-haiku-4-5").available_context() == 200_000

    def test_available_context_override_wins(self) -> None:
        """Explicit context_window kwarg takes precedence over per-model default."""
        adapter = AnthropicAdapter(default_model="claude-opus-4-7", context_window=50_000)
        assert adapter.available_context() == 50_000


class TestHealthCheck:
    async def test_healthy_when_binary_exists_and_version_exits_zero(self) -> None:
        """health_check returns healthy=True when binary is on PATH and exits 0."""
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"claude 2.1.142\n", b""))

        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            status = await AnthropicAdapter(use_ruflo=False).health_check()

        assert status.healthy is True

    async def test_unhealthy_when_binary_missing(self) -> None:
        """health_check returns healthy=False with 'not found' detail when binary absent."""
        with patch("shutil.which", return_value=None):
            status = await AnthropicAdapter(use_ruflo=False).health_check()

        assert status.healthy is False
        assert "claude" in status.detail
        assert "not found" in status.detail


# ──────────────────────────────────────────────────────────────────────────────
# SDK path helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_sdk_stream(
    text_chunks: list[str],
    stop_reason: str = "end_turn",
    tool_uses: list[Any] | None = None,
) -> Any:
    """Build a mock that mimics the anthropic SDK's messages.stream() context manager."""
    final_content = []
    if text_chunks:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "".join(text_chunks)
        final_content.append(text_block)
    for tu in tool_uses or []:
        final_content.append(tu)

    final_message = MagicMock()
    final_message.stop_reason = stop_reason
    final_message.content = final_content

    async def _text_stream():
        for chunk in text_chunks:
            yield chunk

    stream_obj = MagicMock()
    stream_obj.text_stream = _text_stream()
    stream_obj.get_final_message = AsyncMock(return_value=final_message)

    @asynccontextmanager
    async def _stream_ctx(**_kwargs: Any):
        yield stream_obj

    return _stream_ctx


def _make_tool_use_block(
    tool_id: str = "tu_123",
    name: str = "web_search",
    query: str = "test query",
) -> MagicMock:
    tu = MagicMock()
    tu.type = "tool_use"
    tu.id = tool_id
    tu.name = name
    tu.input = {"query": query}
    return tu


# ──────────────────────────────────────────────────────────────────────────────


class TestExtractSystemPrompt:
    def test_single_system_message(self) -> None:
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="system", content="You are helpful."),
                NexusMessage(role="user", content="hi"),
            ],
            style_profile=StyleProfile(),
        )
        assert (
            adapter._extract_system_prompt(request)
            == NEXUS_STYLE + "\n\nYou are helpful."
        )

    def test_multiple_system_messages_concatenated(self) -> None:
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="system", content="Part one."),
                NexusMessage(role="system", content="Part two."),
                NexusMessage(role="user", content="hi"),
            ],
            style_profile=StyleProfile(),
        )
        assert (
            adapter._extract_system_prompt(request)
            == NEXUS_STYLE + "\n\nPart one.\n\nPart two."
        )

    def test_no_system_message_still_carries_voice(self) -> None:
        adapter = AnthropicAdapter()
        request = _make_request("hi")
        assert adapter._extract_system_prompt(request) == NEXUS_STYLE


class TestBuildApiMessages:
    def test_excludes_system_role(self) -> None:
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="system", content="sys"),
                NexusMessage(role="user", content="hi"),
            ],
            style_profile=StyleProfile(),
        )
        api_msgs = adapter._build_api_messages(request)
        assert len(api_msgs) == 1
        assert api_msgs[0]["role"] == "user"

    def test_strips_opus_tag_from_last_user_message(self) -> None:
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="hello #opus")],
            style_profile=StyleProfile(),
        )
        api_msgs = adapter._build_api_messages(request)
        assert "#opus" not in api_msgs[0]["content"].lower()
        assert "hello" in api_msgs[0]["content"]

    def test_preserves_opus_tag_in_non_last_message(self) -> None:
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="user", content="first #opus"),
                NexusMessage(role="assistant", content="ok"),
                NexusMessage(role="user", content="second"),
            ],
            style_profile=StyleProfile(),
        )
        api_msgs = adapter._build_api_messages(request)
        assert api_msgs[0]["content"] == "first #opus"
        assert api_msgs[2]["content"] == "second"

    def test_user_and_assistant_roles_preserved(self) -> None:
        adapter = AnthropicAdapter()
        request = NexusRequest(
            thread_id="t",
            messages=[
                NexusMessage(role="user", content="u1"),
                NexusMessage(role="assistant", content="a1"),
                NexusMessage(role="user", content="u2"),
            ],
            style_profile=StyleProfile(),
        )
        api_msgs = adapter._build_api_messages(request)
        assert [m["role"] for m in api_msgs] == ["user", "assistant", "user"]


class TestSdkPath:
    """Tests for _complete_stream_sdk (enable_web_tools=True path)."""

    async def test_raises_when_no_api_key(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = AnthropicAdapter(enable_web_tools=True)
        request = _make_request("hi")

        with pytest.raises(Exception, match="ANTHROPIC_API_KEY"):
            async for _ in adapter._complete_stream_sdk(request):
                pass

    async def test_streams_text_then_final_token(self, monkeypatch: Any) -> None:
        """Simple end_turn response: text chunks yielded, then is_final token."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        adapter = AnthropicAdapter(enable_web_tools=True)
        request = _make_request("hello")

        stream_ctx = _make_sdk_stream(["Hello", " world"])
        mock_client = MagicMock()
        mock_client.messages.stream = stream_ctx

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            tokens = [t async for t in adapter._complete_stream_sdk(request)]

        text_tokens = [t for t in tokens if not t.is_final]
        final_tokens = [t for t in tokens if t.is_final]
        assert "".join(t.text for t in text_tokens) == "Hello world"
        assert len(final_tokens) == 1

    async def test_tool_use_executes_web_search_and_continues(
        self, monkeypatch: Any
    ) -> None:
        """tool_use stop_reason → web_search executed → second stream yields answer."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        adapter = AnthropicAdapter(enable_web_tools=True)
        request = _make_request("what's the weather?")

        tu_block = _make_tool_use_block(query="weather today")
        first_stream = _make_sdk_stream([], stop_reason="tool_use", tool_uses=[tu_block])
        second_stream = _make_sdk_stream(["It is sunny."], stop_reason="end_turn")

        call_count = [0]

        @asynccontextmanager
        async def _stream_router(**_kwargs: Any):
            if call_count[0] == 0:
                call_count[0] += 1
                async with first_stream() as s:
                    yield s
            else:
                async with second_stream() as s:
                    yield s

        mock_client = MagicMock()
        mock_client.messages.stream = _stream_router

        with (
            patch("anthropic.AsyncAnthropic", return_value=mock_client),
            patch(
                "orchestrator.tools.registry.dispatch",
                new=AsyncMock(return_value="[1] Weather\nhttp://x.com\nSunny"),
            ),
        ):
            tokens = [t async for t in adapter._complete_stream_sdk(request)]

        text = "".join(t.text for t in tokens if not t.is_final)
        assert "sunny" in text.lower()
        assert any(t.is_final for t in tokens)

    async def test_complete_stream_routes_to_sdk_when_web_tools_enabled(
        self, monkeypatch: Any
    ) -> None:
        """complete_stream dispatches to _complete_stream_sdk when enable_web_tools=True."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        adapter = AnthropicAdapter(enable_web_tools=True)
        request = _make_request("hi")

        collected: list[Any] = []

        async def _fake_sdk(req: Any):
            yield MagicMock(text="answer", is_final=False)
            yield MagicMock(text="", is_final=True)
            collected.append(req)

        with patch.object(adapter, "_complete_stream_sdk", side_effect=_fake_sdk):
            tokens = [t async for t in adapter.complete_stream(request)]

        assert len(collected) == 1
        assert len(tokens) == 2

    async def test_sdk_emits_tool_call_and_result_marker_tokens(
        self, monkeypatch: Any
    ) -> None:
        """_complete_stream_sdk yields tool_call then tool_result marker tokens."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        adapter = AnthropicAdapter(enable_web_tools=True)
        request = _make_request("search for cats")

        tu_block = _make_tool_use_block(name="web_search", query="cats")
        first_stream = _make_sdk_stream([], stop_reason="tool_use", tool_uses=[tu_block])
        second_stream = _make_sdk_stream(["Cats are cute."], stop_reason="end_turn")

        call_count = [0]

        @asynccontextmanager
        async def _stream_router(**_kwargs: Any):
            if call_count[0] == 0:
                call_count[0] += 1
                async with first_stream() as s:
                    yield s
            else:
                async with second_stream() as s:
                    yield s

        mock_client = MagicMock()
        mock_client.messages.stream = _stream_router

        dispatch_result = "[1] Cats\nhttps://cats.com\nFluffy"
        with (
            patch("anthropic.AsyncAnthropic", return_value=mock_client),
            patch(
                "orchestrator.tools.registry.dispatch",
                new=AsyncMock(return_value=dispatch_result),
            ),
        ):
            tokens = [t async for t in adapter._complete_stream_sdk(request)]

        tool_call_tokens = [t for t in tokens if t.tool_call is not None]
        tool_result_tokens = [t for t in tokens if t.tool_result is not None]
        assert len(tool_call_tokens) == 1
        assert tool_call_tokens[0].tool_call["name"] == "web_search"
        assert tool_call_tokens[0].tool_call["input"] == {"query": "cats"}
        assert len(tool_result_tokens) == 1
        assert tool_result_tokens[0].tool_result["name"] == "web_search"
        assert "Cats" in tool_result_tokens[0].tool_result["content"]

    async def test_marker_tokens_have_empty_text(self, monkeypatch: Any) -> None:
        """Tool marker tokens carry empty text so they don't pollute the content buffer."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        adapter = AnthropicAdapter(enable_web_tools=True)
        request = _make_request("fetch page")

        tu_block = _make_tool_use_block(name="web_fetch", query="https://example.com")
        tu_block.input = {"url": "https://example.com"}
        first_stream = _make_sdk_stream([], stop_reason="tool_use", tool_uses=[tu_block])
        second_stream = _make_sdk_stream(["Page summary."], stop_reason="end_turn")

        call_count = [0]

        @asynccontextmanager
        async def _router(**_kwargs: Any):
            if call_count[0] == 0:
                call_count[0] += 1
                async with first_stream() as s:
                    yield s
            else:
                async with second_stream() as s:
                    yield s

        mock_client = MagicMock()
        mock_client.messages.stream = _router

        with (
            patch("anthropic.AsyncAnthropic", return_value=mock_client),
            patch(
                "orchestrator.tools.registry.dispatch",
                new=AsyncMock(return_value="Page content"),
            ),
        ):
            tokens = [t async for t in adapter._complete_stream_sdk(request)]

        for t in tokens:
            if t.tool_call is not None or t.tool_result is not None:
                assert t.text == "", "Marker token must have empty text"
