"""Anthropic adapter: Sonnet 4.6 (default), Opus 4.7 (premium), Haiku 4.5 (summariser).

Phase 2a adds multi-model routing alongside the Phase 1 Sonnet-only path.
Model selection order (first match wins):
  1. Last user message contains '#opus' (case-insensitive) → premium_model (Opus)
  2. selection_hint == "summariser"                       → summariser_model (Haiku)
  3. selection_hint == "premium"                          → premium_model (Opus)
  4. selection_hint == "pin:<model-id>"                   → that exact model string
  5. Otherwise                                            → default_model (Sonnet)

The '#opus' tag is stripped from the message content before it is sent to the
subprocess so the routing signal never leaks into the model's input.

Phase 2a ships only explicit signals. Auto-escalation heuristics are Phase 2b.

Transport layer (unchanged from Phase 1):
  claude --model <resolved> --print --output-format stream-json --verbose --no-session-persistence
  --verbose is REQUIRED alongside stream-json; without it Claude Code exits with an error.
  See OPEN_QUESTIONS.md [2026-05-15] for details.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..prompts import NEXUS_STYLE, RESEARCH_PREAMBLE
from ..types import HealthStatus, NexusRequest, NexusResponse, Token
from .base import AdapterTimeout, AdapterUnavailable

if TYPE_CHECKING:
    from ..heuristics.opus import OpusHeuristic

log = logging.getLogger(__name__)

# Matches '#opus' (case-insensitive) plus any surrounding whitespace
_OPUS_TAG_RE = re.compile(r"\s*#opus\s*", re.IGNORECASE)

# chars-per-token ratios per model (used by count_tokens)
_TOKENIZER_RATIOS: dict[str, float] = {
    "claude-sonnet-4-6": 3.8,
    "claude-opus-4-7": 3.6,
    "claude-haiku-4-5": 3.9,
    "claude-haiku-4-5-20251001": 3.9,
}

# Maximum input tokens per model
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-7": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}


class AnthropicAdapter:
    name = "anthropic"

    def __init__(
        self,
        default_model: str = "claude-sonnet-4-6",
        premium_model: str = "claude-opus-4-7",
        summariser_model: str = "claude-haiku-4-5",
        use_ruflo: bool = False,
        ruflo_version: str | None = None,
        context_window: int | None = None,
        timeout_seconds: int = 120,
        enable_web_tools: bool = False,
        opus_heuristic: "OpusHeuristic | None" = None,
    ) -> None:
        self.default_model = default_model
        self.premium_model = premium_model
        self.summariser_model = summariser_model
        self.use_ruflo = use_ruflo
        self.ruflo_version = ruflo_version
        self._context_window_override = context_window
        self.timeout = timeout_seconds
        self.enable_web_tools = enable_web_tools
        self._opus_heuristic = opus_heuristic

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(self, request: NexusRequest) -> NexusResponse:
        """Single-shot completion. Collects the full stream internally."""
        model = self._resolve_model(request)
        chunks: list[str] = []
        started = datetime.now(UTC)
        async for token in self.complete_stream(request):
            if not token.is_final:
                chunks.append(token.text)
        content = "".join(chunks)
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        return NexusResponse(
            content=content,
            model_id=model,
            auth_mode="subscription",
            estimated_tokens=self.count_tokens(content),
            duration_ms=duration_ms,
            finish_reason="stop",
        )

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """Stream tokens. Routes to SDK path when enable_web_tools=True."""
        if self.enable_web_tools:
            async for sdk_token in self._complete_stream_sdk(request):
                yield sdk_token
            return

        if self.use_ruflo:
            raise AdapterUnavailable(
                "use_ruflo=True is not functional in Phase 1. "
                "Ruflo v3.7+ has no 'claude-code' subcommand. "
                "Set use_ruflo: false in Config/cascade.yaml. "
                "See OPEN_QUESTIONS.md."
            )

        prompt = self._build_prompt_text(request)
        cmd = self._build_command(request)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            assert proc.stdin is not None  # guaranteed by PIPE
            assert proc.stdout is not None
            assert proc.stderr is not None

            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=float(self.timeout)
                    )
                except TimeoutError:
                    raise AdapterTimeout(
                        f"claude timed out after {self.timeout}s"
                    )

                if not line_bytes:  # EOF
                    break

                line = line_bytes.decode("utf-8").strip()
                token = self._parse_stream_line(line)
                if token is None:
                    continue
                yield token
                if token.is_final:
                    break

            await asyncio.wait_for(proc.wait(), timeout=5.0)

            if proc.returncode != 0:
                stderr_data = await proc.stderr.read()
                raise AdapterUnavailable(
                    f"claude exited {proc.returncode}: "
                    f"{stderr_data.decode('utf-8', errors='replace').strip()}"
                )

        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    proc.kill()

    def count_tokens(self, text: str) -> int:
        """Approximate token count via chars / model-specific ratio."""
        ratio = _TOKENIZER_RATIOS.get(self.default_model, 3.8)
        return max(1, int(len(text) / ratio))

    def available_context(self) -> int:
        if self._context_window_override is not None:
            return self._context_window_override
        return _CONTEXT_WINDOWS.get(self.default_model, 200_000)

    def estimated_cost(self, request: NexusRequest) -> dict:  # type: ignore[type-arg]
        """Phase 2a: returns zeros. Real quota tracking lands in Phase 4."""
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self) -> HealthStatus:
        binary = "ruflo" if self.use_ruflo else "claude"
        if shutil.which(binary) is None:
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(UTC),
                detail=f"{binary} not found on PATH",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0:
                return HealthStatus(healthy=True, last_checked=datetime.now(UTC))
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(UTC),
                detail=f"{binary} --version exit {proc.returncode}",
            )
        except TimeoutError:
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(UTC),
                detail=f"{binary} --version timed out",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, request: NexusRequest) -> str:
        """Decide which model to invoke for this request.

        Priority order:
          1. #opus tag in last user message → premium (Opus)
          2. selection_hint="summariser"    → summariser (Haiku)  ← heuristic never reached
          3. selection_hint="premium"       → premium (Opus)
          4. selection_hint="pin:<model>"   → that exact model
          5. opus_heuristic fires           → premium (Opus)
          6. default                        → default_model (Sonnet)
        """
        last_user = next(
            (m for m in reversed(request.messages) if m.role == "user"), None
        )
        if last_user and "#opus" in last_user.content.lower():
            return self.premium_model

        hint = request.selection_hint
        if hint == "summariser":
            return self.summariser_model
        if hint == "premium":
            return self.premium_model
        if hint is not None and hint.startswith("pin:"):
            return hint[4:]

        if self._opus_heuristic is not None and last_user is not None:
            if self._opus_heuristic.should_escalate(last_user.content):
                return self.premium_model

        return self.default_model

    def _build_command(self, request: NexusRequest) -> list[str]:
        """Build subprocess argv. use_ruflo=True returns a stub command — see OPEN_QUESTIONS.md."""
        model = self._resolve_model(request)
        if self.use_ruflo:
            base = ["npx", "--yes", "ruflo", "claude-code"]
        else:
            base = ["claude"]
        # Nexus voice on every subprocess call; research preamble concatenates
        # after it when the engine resolved this turn to research mode.
        appended_system = NEXUS_STYLE
        if request.mode == "research":
            appended_system += "\n\n" + RESEARCH_PREAMBLE
        return [
            *base,
            "--model", model,
            "--print",
            "--output-format", "stream-json",
            "--verbose",               # required; without this, stream-json is rejected
            "--no-session-persistence",
            "--append-system-prompt", appended_system,
        ]

    def _build_prompt_text(self, request: NexusRequest) -> str:
        """Flatten messages into a text prompt. Strips #opus tag from last user message."""
        messages = request.messages

        # Find the last user message index for tag stripping
        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"),
            None,
        )

        def _content(i: int, content: str) -> str:
            if i == last_user_idx:
                return _OPUS_TAG_RE.sub(" ", content).strip()
            return content

        turn_messages = [m for m in messages if m.role in ("user", "assistant")]
        parts: list[str] = []

        for msg in messages:
            if msg.role == "system":
                parts.append(msg.content)

        if len(turn_messages) == 1 and turn_messages[0].role == "user":
            # Single user turn — find its index in original messages
            idx = next(i for i, m in enumerate(messages) if m.role == "user")
            parts.append(_content(idx, turn_messages[0].content))
        else:
            for i, msg in enumerate(messages):
                if msg.role not in ("user", "assistant"):
                    continue
                label = "Human" if msg.role == "user" else "Assistant"
                parts.append(f"{label}: {_content(i, msg.content)}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # SDK path (enable_web_tools=True)
    # ------------------------------------------------------------------

    async def _complete_stream_sdk(self, request: NexusRequest) -> AsyncIterator[Token]:
        """Anthropic SDK streaming path used when enable_web_tools=True.

        Handles multi-turn tool use: streams text incrementally, dispatches
        tool calls via the tool registry, then continues streaming.
        Requires ANTHROPIC_API_KEY env var.
        """
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise AdapterUnavailable(
                "anthropic SDK not installed; add 'anthropic' to dependencies"
            ) from exc

        from ..tools.registry import TOOLS  # noqa: PLC0415
        from ..tools.registry import dispatch as dispatch_tool

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise AdapterUnavailable(
                "enable_web_tools=True requires ANTHROPIC_API_KEY environment variable"
            )

        model = self._resolve_model(request)
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=float(self.timeout))
        system_prompt = self._extract_system_prompt(request)
        messages: list[dict[str, Any]] = self._build_api_messages(request)

        while True:
            create_kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": 4096,
                "messages": messages,
                "tools": TOOLS,
            }
            if system_prompt:
                create_kwargs["system"] = system_prompt

            async with client.messages.stream(**create_kwargs) as stream:
                async for text_chunk in stream.text_stream:
                    yield Token(text=text_chunk, is_final=False)
                final_message = await stream.get_final_message()

            if final_message.stop_reason != "tool_use":
                yield Token(text="", is_final=True)
                return

            tool_uses = [b for b in final_message.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": final_message.content})

            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                tu_input: dict[str, Any] = tu.input
                yield Token(text="", tool_call={"name": tu.name, "input": dict(tu_input)})
                result_text = await dispatch_tool(tu.name, tu_input)
                yield Token(text="", tool_result={"name": tu.name, "content": result_text})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_text,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

    def _extract_system_prompt(self, request: NexusRequest) -> str:
        """Concatenate the Nexus voice + all system messages into a single string."""
        parts = [NEXUS_STYLE] + [m.content for m in request.messages if m.role == "system"]
        return "\n\n".join(parts)

    def _build_api_messages(self, request: NexusRequest) -> list[dict[str, Any]]:
        """Convert NexusRequest messages to Anthropic API message list.

        Excludes system messages (passed separately as 'system' kwarg).
        Strips the #opus routing tag from the last user message content.
        """
        last_user_idx = next(
            (
                i
                for i in range(len(request.messages) - 1, -1, -1)
                if request.messages[i].role == "user"
            ),
            None,
        )
        api_messages: list[dict[str, Any]] = []
        for i, msg in enumerate(request.messages):
            if msg.role == "system":
                continue
            content = msg.content
            if i == last_user_idx:
                content = _OPUS_TAG_RE.sub(" ", content).strip()
            api_messages.append({"role": msg.role, "content": content})
        return api_messages

    def _parse_stream_line(self, line: str) -> Token | None:
        """Parse one line of stream-json output into a Token, or None to skip.

        Format verified against Claude Code v2.1.142 on 2026-05-15.
        See OPEN_QUESTIONS.md entry 'stream-json event format'.
        """
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.debug("Non-JSON line from claude (ignored): %.120s", line)
            return None

        msg_type = obj.get("type")

        if msg_type == "assistant":
            message = obj.get("message", {})
            content_blocks = message.get("content", [])
            texts = [
                block["text"]
                for block in content_blocks
                if block.get("type") == "text" and "text" in block
            ]
            if texts:
                return Token(text="".join(texts), is_final=False)
            return None  # thinking block or other non-text content

        if msg_type == "result":
            if obj.get("is_error") or obj.get("subtype") != "success":
                detail = obj.get("result") or obj.get("error") or str(obj)
                raise AdapterUnavailable(f"Claude Code error result: {detail}")
            return Token(text="", is_final=True)

        # system/init, rate_limit_event, and future event types — skip silently
        log.debug("Skipping claude stream event type=%s", msg_type)
        return None
