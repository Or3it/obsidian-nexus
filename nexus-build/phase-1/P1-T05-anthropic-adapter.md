# P1-T05 — Anthropic Adapter (Claude Code via Ruflo + Direct Fallback)

## Goal

Implement the only adapter that exists in Phase 1: Claude Sonnet 4.6 reached via Claude Code, wrapped by Ruflo (default), authenticated via the user's Claude Pro subscription. Support a config-controlled fallback path that invokes Claude Code directly when `use_ruflo: false`.

This is the largest Phase 1 ticket. Budget the most time for it.

## Read first

- `shared/adapter-protocol.md` (the full Adapter contract)
- `shared/risks-to-verify.md` (Ruflo version, Claude Code flags, model strings)
- The Anthropic adapter section in the PRD (lines 648–654)

## Files to create or replace

```
orchestrator/adapters/base.py           ← Adapter Protocol definition
orchestrator/adapters/anthropic.py      ← the implementation
tests/test_anthropic_adapter.py
tests/fixtures/claude_code_stdout.txt   ← sample line-oriented JSON from claude
tests/fixtures/ruflo_stdout.txt         ← sample from npx ruflo claude-code
```

## Verification step — HARD GATE — DO THIS BEFORE WRITING ANY CODE

Per `shared/risks-to-verify.md`. This step is a **hard gate**: do not proceed to implementation until it is complete. If you cannot complete a step, stop the session and ask the user for the verification output. **Do not proceed on guesses.** The PRD's claimed CLI surface for Ruflo and Claude Code is educated guessing, not measured fact — it will be wrong in detail, and the adapter will fail at integration time if you encode the guesses as truth.

**Step 1 — npm package check.** Run `npm view @anthropic-ai/claude-code version`. Confirm the package exists and record the version.

**Step 2 — Ruflo package check.** Run `npm view ruflo version`. If the package doesn't exist, has been renamed, or has significantly diverged from the PRD's description, **stop, append to `OPEN_QUESTIONS.md`, and notify the user**. Do not proceed with guesses about Ruflo's CLI surface.

**Step 3 — HARD GATE: Claude Code flag check.** Run `claude --help`. If `claude` is not on `PATH` or `--help` fails, **stop the session and ask the user to provide the output of `claude --help` from a machine that has it installed.** Do NOT proceed with assumed flags from the PRD. The PRD's `--model` / `--json` / `--message` / `--print` are guesses, not facts. Specifically capture:

- The actual flag name for selecting the model.
- The actual flag name for printing/non-interactive mode.
- Whether the prompt is passed as an argument, on stdin, or via a file.
- The actual output format flag (the PRD claims `--json`; verify).
- The actual login command (the PRD claims `claude /login`; verify).

**Step 4 — HARD GATE: Ruflo CLI surface check.** Run `npx ruflo --help` (or `ruflo --help` if installed globally). Same rule: if it fails, stop the session and ask the user. Do not assume the subcommand structure. Specifically capture:

- Whether Ruflo wraps Claude Code as a subcommand (`npx ruflo claude-code ...`) or differently.
- How Ruflo's flags compose with Claude Code's flags.
- The exit codes Ruflo uses to signal failure.

**Step 5 — Model identifier check.** Confirm current Claude model identifiers at https://docs.claude.com/en/docs/about-claude/models. The PRD writes `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5`. Verify these are the actual model IDs. Note that the Claude Code CLI argument format may differ from the API model string — capture both.

**Recording requirements.** Append all findings to `OPEN_QUESTIONS.md` at the repo root, even when verification succeeds — Phase 1 acceptance review needs the trail. If any verification step is skipped or returns ambiguous results, log that fact explicitly and proceed only with the user's explicit go-ahead.

## `orchestrator/adapters/base.py`

The Protocol from `shared/adapter-protocol.md`, plus an abstract base class implementing safe defaults:

```python
from typing import Protocol, AsyncIterator, runtime_checkable
from datetime import datetime, timezone
from ..types import NexusRequest, NexusResponse, Token, HealthStatus

@runtime_checkable
class Adapter(Protocol):
    name: str
    async def complete(self, request: NexusRequest) -> NexusResponse: ...
    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]: ...
    def count_tokens(self, text: str) -> int: ...
    def available_context(self) -> int: ...
    def estimated_cost(self, request: NexusRequest) -> dict: ...
    async def health_check(self) -> HealthStatus: ...

class AdapterError(Exception):
    """Adapter-level error. Subclasses describe the failure class."""

class AdapterUnavailable(AdapterError): ...
class AdapterQuotaExhausted(AdapterError): ...
class AdapterTimeout(AdapterError): ...
class AdapterRefusal(AdapterError): ...
```

## `orchestrator/adapters/anthropic.py`

Implements the protocol via subprocess.

```python
import asyncio
import json
import shutil
from datetime import datetime, timezone
from typing import AsyncIterator
from ..types import NexusRequest, NexusResponse, Token, HealthStatus
from .base import AdapterUnavailable, AdapterTimeout, AdapterQuotaExhausted

class AnthropicAdapter:
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        use_ruflo: bool = True,
        ruflo_version: str | None = None,
        tokenizer_ratio: float = 3.8,
        context_window: int = 200_000,
        timeout_seconds: int = 120,
    ):
        self.model = model
        self.use_ruflo = use_ruflo
        self.ruflo_version = ruflo_version
        self.tokenizer_ratio = tokenizer_ratio
        self.context_window = context_window
        self.timeout = timeout_seconds

    async def complete(self, request: NexusRequest) -> NexusResponse:
        chunks: list[str] = []
        started = datetime.now(timezone.utc)
        async for token in self.complete_stream(request):
            chunks.append(token.text)
        content = "".join(chunks)
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return NexusResponse(
            content=content,
            model_id=self.model,
            auth_mode="subscription",
            estimated_tokens=self.count_tokens(content),
            duration_ms=duration_ms,
            finish_reason="stop",
        )

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        cmd = self._build_command(request)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            # Send the prompt on stdin if the CLI uses stdin; otherwise embed in args.
            # CLI flag conventions are VERIFIED via the hard gate above and recorded
            # in OPEN_QUESTIONS.md — use those, not the PRD's guesses.
            ...
            async for line in proc.stdout:
                token = self._parse_stream_line(line.decode("utf-8").strip())
                if token:
                    yield token
                if token and token.is_final:
                    break
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

    def _build_command(self, request: NexusRequest) -> list[str]:
        """
        Build the subprocess args for Ruflo+Claude Code, or direct Claude Code.

        IMPORTANT: The flag names below MUST match what was captured during the
        verification hard gate (see OPEN_QUESTIONS.md). The PRD's claimed flags
        (--model, --json, --message) are NOT to be assumed correct — read the
        actual values from the verification step.
        """
        if self.use_ruflo:
            base = ["npx", "ruflo", "claude-code"]   # adjust per verified Ruflo CLI
        else:
            base = ["claude"]
        # Append --model, prompt-source flags, json output, etc. per current claude --help.
        ...

    def _parse_stream_line(self, line: str) -> Token | None:
        """
        Claude Code's streaming-output mode emits one JSON object per line.
        Extract the text delta and final-marker. The exact JSON shape
        was captured during verification — see tests/fixtures/claude_code_stdout.txt.
        """
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        # Shape captured during verification. Likely something like:
        #   {"type": "content_block_delta", "delta": {"text": "..."}}
        # with {"type": "message_stop"} for final. Confirm against fixture.
        ...

    def count_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.tokenizer_ratio))

    def available_context(self) -> int:
        return self.context_window

    def estimated_cost(self, request: NexusRequest) -> dict:
        # Subscription mode: no USD cost; estimate Pro quota burn from input + projected output.
        input_tokens = sum(self.count_tokens(m.content) for m in request.messages)
        # Phase 1 returns 0.0 for pro_quota_pct; real quota tracking comes in Phase 2/4.
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self) -> HealthStatus:
        binary = "ruflo" if self.use_ruflo else "claude"
        if shutil.which(binary) is None:
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(timezone.utc),
                detail=f"{binary} not found on PATH",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                return HealthStatus(healthy=True, last_checked=datetime.now(timezone.utc))
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(timezone.utc),
                detail=f"{binary} --version exit {proc.returncode}",
            )
        except asyncio.TimeoutError:
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(timezone.utc),
                detail=f"{binary} --version timed out",
            )
```

## Critical implementation notes

1. **The PRD's claimed flags are guesses, not facts.** The line `claude --model claude-sonnet-4-6 --json --message "{prompt}"` is plausible but unverified. The verification hard gate above produces the real flags. Encode those, not the guesses.
2. **Long prompts via stdin, not args.** OS-level argv length limits will bite long prompts. Pass the prompt on stdin if Claude Code supports it (`claude --print -` style or whatever was discovered); otherwise write to a temp file and pass its path. Don't embed multi-KB strings as argv.
3. **Streaming is line-oriented JSON** (per PRD claim, verified during the hard gate). Use a JSON-streaming line parser; tolerate non-JSON lines (debug output, warnings) by ignoring them with a debug log.
4. **Subprocess cleanup is mandatory.** Use a `try`/`finally` to terminate or kill the subprocess on every exit path. A hung Claude Code subprocess will leak file descriptors and PIDs.
5. **The Ruflo wrapper is unstable.** Per PRD: "Ruflo's CLI surface is mid-v3 rewrite." If `use_ruflo: false` is set, fall through to direct invocation. The two code paths must be selectable purely by config.
6. **No token meter in subscription mode.** Approximate via character count → tokens. Calibrate the ratio at first run (a Phase 2 ticket); Phase 1 uses the static 3.8 default for Sonnet 4.6.
7. **Capture real stdout for tests.** When Claude Code is available, capture a real session's streaming-mode stdout to `tests/fixtures/claude_code_stdout.txt`. Mock-test the parser against this fixture. **If Claude Code isn't available at ticket time, stop the session** — see the hard gate. Do not synthesise plausible fixtures and proceed.

## Tests (`tests/test_anthropic_adapter.py`)

Mock the subprocess with `asyncio.subprocess` test helpers (or use `monkeypatch` to patch `asyncio.create_subprocess_exec`). The tests should NOT actually call Claude Code; they verify the adapter's behavior given known stdout/stderr.

- **`complete_stream` yields tokens in order** matching the fixture stdout.
- **`complete` returns a `NexusResponse` whose `content` equals concatenated `complete_stream` text.**
- **Empty stdout yields zero tokens, `finish_reason="stop"`** (this is the well-behaved empty case).
- **Subprocess timeout** raises `AdapterTimeout` (use a mock that hangs longer than the configured timeout).
- **Subprocess non-zero exit** raises `AdapterUnavailable` with the stderr included in the message.
- **`use_ruflo: false` switches the command base** from `["npx", "ruflo", ...]` to `["claude", ...]` — verifiable via a `_build_command` unit test, no subprocess needed.
- **`count_tokens("hello world")` returns approximately `len(text) / 3.8`.**
- **`available_context()` returns the configured context window.**
- **`health_check` returns `healthy=True` when the binary exists and `--version` exits 0.**
- **`health_check` returns `healthy=False` with a useful detail when the binary is missing.**

## Definition of done

- [ ] **Verification hard gate (steps 1–5) completed and findings recorded in `OPEN_QUESTIONS.md`.** If any step was skipped, the user explicitly approved skipping it.
- [ ] `base.py` and `anthropic.py` exist and conform to `shared/adapter-protocol.md`.
- [ ] All adapter tests pass against mocked subprocess.
- [ ] **Manual smoke test:** running `python -c "import asyncio; from orchestrator.adapters.anthropic import AnthropicAdapter; ...; asyncio.run(adapter.complete(req))"` against a real Claude Code installation streams a response from Sonnet 4.6. Document the exact invocation in `tests/manual_smoke.md`.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't implement Opus or Haiku routing logic. The adapter accepts `model` as a parameter; the orchestrator decides which model to ask for, and in Phase 1 it always asks for Sonnet.
- Don't implement Pro quota tracking. `estimated_cost` returns `{"pro_quota_pct": 0.0, "usd": 0.0}` — real tracking lands in Phase 4.
- Don't implement the trigger taxonomy. The adapter raises `AdapterError` subclasses; the orchestrator's cascade engine (Phase 2) maps them to trigger classes.
- Don't pin to a specific Ruflo version yet — read it from `Config/ruflo.lock` if present, fall back to "latest" otherwise. Pinning logic is a Phase 5 onboarding concern.
