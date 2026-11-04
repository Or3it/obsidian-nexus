"""Tests for cascade/engine.py — CascadeEngine.

All tests bypass __init__ via _bare_engine() to avoid real adapter construction,
watchdog threads, and file-system config loading. Only the select_rung /
failover / inject logic is exercised.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.adapters.base import (
    AdapterModelUnavailable,
    AdapterQuotaExhausted,
    AdapterRateLimit,
    AdapterUnavailable,
)
from orchestrator.cascade.cooldown import CooldownLedger
from orchestrator.cascade.engine import CascadeEngine
from orchestrator.cascade.events import (
    ErrorEvent,
    HandoverEvent,
    MetaEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from orchestrator.cascade.model_health import ModelHealth
from orchestrator.cascade.triggers import LatencyTracker
from orchestrator.types import (
    CascadeConfig,
    FloorConfig,
    HealthStatus,
    NexusMessage,
    NexusRequest,
    PoolConfig,
    PoolMember,
    RungConfig,
    StyleProfile,
    Token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _two_rung_config() -> CascadeConfig:
    return CascadeConfig(
        schema_version=2,
        rungs=[
            RungConfig(
                position=1,
                adapter="anthropic",
                auth_mode="subscription",
                models={"default": "claude-sonnet-4-6", "premium": "claude-opus-4-7"},
            ),
            RungConfig(
                position=2,
                adapter="openrouter",
                auth_mode="api_key",
                model="minimax/minimax-m1",
            ),
        ],
    )


def _bare_engine(tmp_path: Path, config: CascadeConfig | None = None) -> CascadeEngine:
    """Build a CascadeEngine without calling __init__."""
    eng: CascadeEngine = object.__new__(CascadeEngine)
    eng.vault_root = tmp_path
    eng.config_path = tmp_path / "cascade.yaml"
    eng.budgets_path = tmp_path / "Orchestrator" / "Config" / "budgets.yaml"
    eng.cooldowns = CooldownLedger(eng.budgets_path)
    eng.model_health = ModelHealth(tmp_path / "Orchestrator" / "Config" / "model_health.json")
    eng.latency = LatencyTracker()
    eng._adapters: dict[int, object] = {}
    eng._pool_adapters: dict[str, object] = {}
    eng._floor_adapter = None
    eng._probe_cache: dict[str, datetime] = {}
    eng._summariser = None
    eng._reload_lock = asyncio.Lock()
    eng._loop = None
    eng._config = config or _two_rung_config()
    eng._watcher = MagicMock()
    return eng


def _simple_request(
    thread_id: str = "test-thread-abcdef12",
    mode: str = "hard",
) -> NexusRequest:
    """Request with explicit mode="hard" (Sonnet-first order) by default.

    The health/failover tests below exercise selection mechanics, not mode
    policy; hard mode reproduces the pre-3W ordering. Mode policy itself is
    covered by TestModePolicy.
    """
    return NexusRequest(
        thread_id=thread_id,
        messages=[
            NexusMessage(role="system", content="You are helpful."),
            NexusMessage(role="user", content="Hello"),
        ],
        style_profile=StyleProfile(),
        mode=mode,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# _cooldown_key
# ---------------------------------------------------------------------------


class TestCooldownKey:
    def test_anthropic_rung_uses_position_key(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._cooldown_key(1) == "rung_1_anthropic"

    def test_openrouter_always_shared_key(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._cooldown_key(2) == "openrouter_shared"

    def test_unknown_position_returns_unknown_key(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._cooldown_key(99) == "rung_99_unknown"


# ---------------------------------------------------------------------------
# _active_model_for_rung
# ---------------------------------------------------------------------------


class TestActiveModelForRung:
    def test_anthropic_rung_returns_default_model(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._active_model_for_rung(1) == "claude-sonnet-4-6"

    def test_openrouter_rung_returns_model_string(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._active_model_for_rung(2) == "minimax/minimax-m1"

    def test_unknown_position_returns_unknown(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._active_model_for_rung(99) == "unknown"


# ---------------------------------------------------------------------------
# select_rung
# ---------------------------------------------------------------------------


class TestSelectRung:
    def test_picks_lowest_available_rung(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng.select_rung({}) == 1

    def test_skips_exhausted_rung(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        eng.cooldowns.mark_exhausted("rung_1_anthropic", 3600)
        assert eng.select_rung({}) == 2

    def test_falls_back_to_last_when_all_exhausted(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        eng.cooldowns.mark_exhausted("rung_1_anthropic", 3600)
        eng.cooldowns.mark_exhausted("openrouter_shared", 3600)
        # All exhausted — last rung is the final hope.
        assert eng.select_rung({}) == 2

    def test_pinned_model_routes_to_matching_rung(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        thread_yaml = {"pinned_model": "claude-opus-4-7"}
        assert eng.select_rung(thread_yaml) == 1

    def test_unknown_pinned_model_falls_back_to_lowest(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        thread_yaml = {"pinned_model": "no-such-model-xyz"}
        # No rung matches, so falls through to normal selection.
        assert eng.select_rung(thread_yaml) == 1


# ---------------------------------------------------------------------------
# _next_rung_position
# ---------------------------------------------------------------------------


class TestNextRungPosition:
    def test_returns_next_position(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._next_rung_position(1) == 2

    def test_returns_none_when_no_next_rung(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        assert eng._next_rung_position(2) is None

    def test_skips_exhausted_next_rung(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        eng.cooldowns.mark_exhausted("openrouter_shared", 3600)
        assert eng._next_rung_position(1) is None


# ---------------------------------------------------------------------------
# _inject_handover
# ---------------------------------------------------------------------------


class TestInjectHandover:
    def test_none_context_returns_unchanged_request(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        req = _simple_request()
        result = eng._inject_handover(req, None)
        assert result is req

    def test_empty_context_returns_unchanged_request(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        req = _simple_request()
        result = eng._inject_handover(req, "")
        assert result is req

    def test_prepends_to_existing_system_message(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        req = _simple_request()
        result = eng._inject_handover(req, "HANDOVER BRIEF")
        assert result.messages[0].role == "system"
        assert result.messages[0].content.startswith("HANDOVER BRIEF")
        assert "You are helpful." in result.messages[0].content

    def test_inserts_system_when_none_present(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        req = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="Hi")],
            style_profile=StyleProfile(),
        )
        result = eng._inject_handover(req, "HANDOVER BRIEF")
        assert result.messages[0].role == "system"
        assert result.messages[0].content == "HANDOVER BRIEF"
        assert result.messages[1].role == "user"

    def test_does_not_mutate_original_request(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        req = _simple_request()
        original_content = req.messages[0].content
        eng._inject_handover(req, "SOME CONTEXT")
        assert req.messages[0].content == original_content


# ---------------------------------------------------------------------------
# _fallback_handover
# ---------------------------------------------------------------------------


class TestFallbackHandover:
    def test_creates_file_in_handovers_dir(self, tmp_path) -> None:
        from orchestrator.cascade.triggers import TriggerClass, TriggerOutcome

        eng = _bare_engine(tmp_path)
        outcome = TriggerOutcome(
            trigger=TriggerClass.HARD_FAIL,
            retryable=False,
            failover=True,
            detail="adapter unavailable: connection refused",
        )
        path = eng._fallback_handover(
            thread_id="abc12345-0000-0000-0000-000000000000",
            sequence=1,
            from_model="claude-sonnet-4-6",
            to_model="minimax/minimax-m1",
            trigger_outcome=outcome,
            transcript=[NexusMessage(role="user", content="What is 2+2?")],
            full_history_filename="2026-05-18-test.md",
        )
        assert path.exists()
        assert path.parent == tmp_path / "Orchestrator" / "Handovers"

    def test_frontmatter_fields_are_correct(self, tmp_path) -> None:
        import yaml

        from orchestrator.cascade.triggers import TriggerClass, TriggerOutcome

        eng = _bare_engine(tmp_path)
        outcome = TriggerOutcome(
            trigger=TriggerClass.HARD_FAIL,
            retryable=False,
            failover=True,
            detail="down",
        )
        path = eng._fallback_handover(
            thread_id="deadbeef-0000-0000-0000-000000000000",
            sequence=2,
            from_model="claude-sonnet-4-6",
            to_model="minimax/minimax-m1",
            trigger_outcome=outcome,
            transcript=[],
            full_history_filename="thread.md",
        )
        text = path.read_text(encoding="utf-8")
        end = text.index("\n---\n", 4)
        fm = yaml.safe_load(text[4:end])
        assert fm["type"] == "handover"
        assert fm["schema_version"] == 2
        assert fm["from_model"] == "claude-sonnet-4-6"
        assert fm["to_model"] == "minimax/minimax-m1"
        assert fm["trigger"] == "HARD_FAIL"
        assert fm["sequence"] == 2
        assert fm["summariser_model"] == "fallback-template"

    def test_body_includes_last_user_message(self, tmp_path) -> None:
        from orchestrator.cascade.triggers import TriggerClass, TriggerOutcome

        eng = _bare_engine(tmp_path)
        outcome = TriggerOutcome(
            trigger=TriggerClass.HARD_FAIL, retryable=False, failover=True, detail="d"
        )
        path = eng._fallback_handover(
            thread_id="aaaabbbb-0000-0000-0000-000000000000",
            sequence=1,
            from_model="a",
            to_model="b",
            trigger_outcome=outcome,
            transcript=[NexusMessage(role="user", content="What is the meaning of life?")],
            full_history_filename="t.md",
        )
        assert "What is the meaning of life?" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# complete_stream — happy path
# ---------------------------------------------------------------------------


class TestCompleteStreamHappyPath:
    async def test_yields_token_events(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)

        async def _gen(req):  # type: ignore[no-untyped-def]
            yield Token(text="Hello")
            yield Token(text=" world", is_final=True)

        mock_adapter = MagicMock()
        mock_adapter.name = "anthropic"
        mock_adapter.complete_stream = _gen
        eng._adapters[1] = mock_adapter

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "history.md")]

        token_events = [e for e in events if isinstance(e, TokenEvent)]
        assert len(token_events) == 2
        assert token_events[0].token.text == "Hello"
        assert token_events[1].token.text == " world"

    async def test_no_error_events_on_success(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)

        async def _gen(req):  # type: ignore[no-untyped-def]
            yield Token(text="ok", is_final=True)

        mock_adapter = MagicMock()
        mock_adapter.name = "anthropic"
        mock_adapter.complete_stream = _gen
        eng._adapters[1] = mock_adapter

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "history.md")]
        assert not any(isinstance(e, ErrorEvent) for e in events)


# ---------------------------------------------------------------------------
# complete_stream — failover path
# ---------------------------------------------------------------------------


class TestCompleteStreamFailover:
    async def test_failover_yields_handover_then_tokens(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)

        async def _failing(req):  # type: ignore[no-untyped-def]
            raise AdapterUnavailable("model not found")
            yield  # makes this an async generator

        async def _succeeding(req):  # type: ignore[no-untyped-def]
            yield Token(text="fallback reply", is_final=True)

        mock1 = MagicMock()
        mock1.name = "anthropic"
        mock1.complete_stream = _failing

        mock2 = MagicMock()
        mock2.name = "openrouter"
        mock2.complete_stream = _succeeding

        eng._adapters = {1: mock1, 2: mock2}

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "history.md")]

        handover_events = [e for e in events if isinstance(e, HandoverEvent)]
        token_events = [e for e in events if isinstance(e, TokenEvent)]

        assert len(handover_events) == 1
        assert handover_events[0].from_rung == 1
        assert handover_events[0].to_rung == 2
        assert handover_events[0].trigger == "HARD_FAIL"
        assert len(token_events) == 1
        assert token_events[0].token.text == "fallback reply"

    async def test_handover_doc_written_to_vault(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)

        async def _failing(req):  # type: ignore[no-untyped-def]
            raise AdapterUnavailable("down")
            yield

        async def _ok(req):  # type: ignore[no-untyped-def]
            yield Token(text="ok", is_final=True)

        mock1 = MagicMock()
        mock1.name = "anthropic"
        mock1.complete_stream = _failing
        mock2 = MagicMock()
        mock2.name = "openrouter"
        mock2.complete_stream = _ok
        eng._adapters = {1: mock1, 2: mock2}

        async for _ in eng.complete_stream(_simple_request(), {}, "history.md"):
            pass

        handovers_dir = tmp_path / "Orchestrator" / "Handovers"
        docs = list(handovers_dir.glob("*.md"))
        assert len(docs) == 1

    async def test_all_rungs_exhausted_yields_error(self, tmp_path) -> None:
        eng = _bare_engine(tmp_path)
        # Only one rung — no next rung available after failure.
        eng._config = CascadeConfig(
            schema_version=2,
            rungs=[RungConfig(position=1, adapter="anthropic", auth_mode="subscription")],
        )

        async def _failing(req):  # type: ignore[no-untyped-def]
            raise AdapterUnavailable("down")
            yield

        mock1 = MagicMock()
        mock1.name = "anthropic"
        mock1.complete_stream = _failing
        eng._adapters = {1: mock1}

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "history.md")]

        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert error_events[0].terminal


# ---------------------------------------------------------------------------
# complete_stream — tool-call event translation (P2b-T05)
# ---------------------------------------------------------------------------


class TestCompleteStreamToolEvents:
    async def test_tool_call_token_becomes_tool_call_event(self, tmp_path) -> None:
        """A Token with tool_call set is translated to ToolCallEvent, not TokenEvent."""
        eng = _bare_engine(tmp_path)

        async def _gen(req):  # type: ignore[no-untyped-def]
            yield Token(text="", tool_call={"name": "web_search", "input": {"query": "q"}})
            yield Token(text="answer", is_final=True)

        mock_adapter = MagicMock()
        mock_adapter.name = "anthropic"
        mock_adapter.complete_stream = _gen
        eng._adapters[1] = mock_adapter

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "web_search"
        assert tool_calls[0].input == {"query": "q"}

        # The marker token must NOT appear as a TokenEvent
        token_texts = [e.token.text for e in events if isinstance(e, TokenEvent)]
        assert "" not in token_texts or all(
            e.token.tool_call is None for e in events if isinstance(e, TokenEvent)
        )

    async def test_tool_result_token_becomes_tool_result_event(self, tmp_path) -> None:
        """A Token with tool_result set is translated to ToolResultEvent, not TokenEvent."""
        eng = _bare_engine(tmp_path)

        async def _gen(req):  # type: ignore[no-untyped-def]
            yield Token(text="", tool_result={"name": "web_search", "content": "Sunny"})
            yield Token(text="It is sunny.", is_final=True)

        mock_adapter = MagicMock()
        mock_adapter.name = "anthropic"
        mock_adapter.complete_stream = _gen
        eng._adapters[1] = mock_adapter

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 1
        assert tool_results[0].name == "web_search"
        assert "Sunny" in tool_results[0].content

    async def test_tool_marker_tokens_not_in_token_events(self, tmp_path) -> None:
        """Marker tokens are consumed as ToolCallEvent/ToolResultEvent, never TokenEvent."""
        eng = _bare_engine(tmp_path)

        async def _gen(req):  # type: ignore[no-untyped-def]
            yield Token(text="", tool_call={"name": "web_fetch", "input": {"url": "https://x.com"}})
            yield Token(text="", tool_result={"name": "web_fetch", "content": "Page content"})
            yield Token(text="Summary.", is_final=True)

        mock_adapter = MagicMock()
        mock_adapter.name = "anthropic"
        mock_adapter.complete_stream = _gen
        eng._adapters[1] = mock_adapter

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        assert sum(1 for e in events if isinstance(e, ToolCallEvent)) == 1
        assert sum(1 for e in events if isinstance(e, ToolResultEvent)) == 1
        token_events = [e for e in events if isinstance(e, TokenEvent)]
        assert len(token_events) == 1
        assert token_events[0].token.text == "Summary."


# ---------------------------------------------------------------------------
# Pool config helpers (P2c-T04)
# ---------------------------------------------------------------------------


def _pool_config(
    frontier: list[str] | None = None,
    strong: list[str] | None = None,
    reliable: list[str] | None = None,
) -> CascadeConfig:
    """Build a schema-3 pool config with the given member slugs."""
    frontier = frontier or ["frontier/a:free", "frontier/b:free"]
    strong = strong or ["strong/c:free"]
    reliable = reliable or ["reliable/d:free"]
    members = (
        [PoolMember(model=m, tier="frontier") for m in frontier]
        + [PoolMember(model=m, tier="strong") for m in strong]
        + [PoolMember(model=m, tier="reliable") for m in reliable]
    )
    return CascadeConfig(
        schema_version=3,
        rungs=[
            RungConfig(
                position=1,
                adapter="anthropic",
                auth_mode="subscription",
                models={"default": "claude-sonnet-4-6"},
            )
        ],
        pool=PoolConfig(
            adapter="openrouter",
            auth_mode="api_key_free_tier",
            tier_order=["frontier", "strong", "reliable"],
            members=members,
        ),
        floor=FloorConfig(adapter="ollama", auth_mode="local", model="gemma4:latest"),
    )


def _mock_adapter(name: str = "openrouter") -> MagicMock:
    m = MagicMock()
    m.name = name
    return m


def _make_stream(*texts: str):  # type: ignore[no-untyped-def]
    """Return an async generator that yields the given texts then a final token."""
    async def _gen(req):  # type: ignore[no-untyped-def]
        for t in texts:
            yield Token(text=t)
        yield Token(text="", is_final=True)
    return _gen


def _failing_stream(exc: Exception):  # type: ignore[no-untyped-def]
    """Return an async generator that immediately raises exc."""
    async def _gen(req):  # type: ignore[no-untyped-def]
        raise exc
        yield  # make it an async generator
    return _gen


def _bare_pool_engine(
    tmp_path: Path,
    config: CascadeConfig | None = None,
) -> CascadeEngine:
    """Pool-aware bare engine with pre-wired mock adapters for the default config."""
    cfg = config or _pool_config()
    eng = _bare_engine(tmp_path, cfg)
    assert cfg.pool is not None

    # Rung 1 — Sonnet (mock)
    sonnet = _mock_adapter("anthropic")
    sonnet.complete_stream = _make_stream("sonnet answer")
    eng._adapters[1] = sonnet

    # Pool members
    pool_mocks: dict[str, MagicMock] = {}
    for member in cfg.pool.members:
        m = _mock_adapter("openrouter")
        m.complete_stream = _make_stream(f"reply from {member.model}")
        pool_mocks[member.model] = m
    eng._pool_adapters = pool_mocks

    # Floor
    floor_mock = _mock_adapter("ollama")
    floor_mock.complete_stream = _make_stream("floor answer")
    eng._floor_adapter = floor_mock

    return eng


# ---------------------------------------------------------------------------
# Pool selection — ordering and skipping
# ---------------------------------------------------------------------------


class TestPoolSelection:
    async def test_sonnet_answers_when_live(self, tmp_path: Path) -> None:
        eng = _bare_pool_engine(tmp_path)
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert "sonnet answer" in texts
        assert not any(isinstance(e, HandoverEvent) for e in events)

    async def test_first_frontier_selected_when_sonnet_unavailable(
        self, tmp_path: Path
    ) -> None:
        eng = _bare_pool_engine(tmp_path)
        # Sonnet fails → should pick first frontier member
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        handovers = [e for e in events if isinstance(e, HandoverEvent)]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert len(handovers) == 1
        assert handovers[0].to_model == "frontier/a:free"
        assert any("frontier/a" in t for t in texts)

    async def test_second_frontier_selected_when_first_dead(
        self, tmp_path: Path
    ) -> None:
        eng = _bare_pool_engine(tmp_path)
        # Mark first frontier dead; make Sonnet fail so pool is entered
        eng.model_health.mark_unavailable("frontier/a:free", "down")
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("frontier/b" in t for t in texts)

    async def test_strong_selected_when_all_frontier_dead(
        self, tmp_path: Path
    ) -> None:
        eng = _bare_pool_engine(tmp_path)
        eng.model_health.mark_unavailable("frontier/a:free", "down")
        eng.model_health.mark_unavailable("frontier/b:free", "down")
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("strong/c" in t for t in texts)

    async def test_falls_through_to_floor_when_all_dead(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        eng = _bare_pool_engine(tmp_path)
        # Mark every candidate dead — including the floor — so _select_pool_candidate
        # returns None and the unconditional floor fallback (with warning) is taken.
        for m in ["claude-sonnet-4-6", "frontier/a:free", "frontier/b:free",
                  "strong/c:free", "reliable/d:free", "gemma4:latest"]:
            eng.model_health.mark_unavailable(m, "down")

        with caplog.at_level(logging.WARNING, logger="orchestrator.cascade.engine"):
            events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("floor" in t for t in texts)
        assert any("degraded" in r.message or "floor" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Pool outcome recording
# ---------------------------------------------------------------------------


class TestPoolOutcomeRecording:
    async def test_mark_success_called_on_clean_stream(self, tmp_path: Path) -> None:
        eng = _bare_pool_engine(tmp_path)
        await asyncio.gather(*[
            asyncio.ensure_future(
                _consume(eng.complete_stream(_simple_request(), {}, "h.md"))
            )
        ])
        # Sonnet succeeded — its model should be marked live (no dead entry)
        assert not eng.model_health.is_dead("claude-sonnet-4-6")

    async def test_model_unavailable_calls_mark_unavailable(
        self, tmp_path: Path
    ) -> None:
        eng = _bare_pool_engine(tmp_path)
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("Provider returned error")
        )
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        handovers = [e for e in events if isinstance(e, HandoverEvent)]
        assert len(handovers) >= 1
        assert handovers[0].reason == "model_unavailable"
        # Sonnet is now marked dead
        assert eng.model_health.is_dead("claude-sonnet-4-6")

    async def test_rate_limit_sets_cooldown_not_health(self, tmp_path: Path) -> None:
        """AdapterRateLimit must NOT increment ModelHealth failure counter."""
        eng = _bare_pool_engine(tmp_path)
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterRateLimit("rate limit exceeded", cooldown_seconds=45)
        )
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        handovers = [e for e in events if isinstance(e, HandoverEvent)]
        assert len(handovers) >= 1
        assert handovers[0].reason == "rate_limited"
        # Health counter must NOT be incremented — Sonnet is NOT dead
        assert not eng.model_health.is_dead("claude-sonnet-4-6")
        entry = eng.model_health._data.get("claude-sonnet-4-6")
        assert entry is None or entry.consecutive_failures == 0
        # But cooldown IS set
        assert eng.cooldowns.is_exhausted("claude-sonnet-4-6")

    async def test_quota_exhausted_sets_shared_cooldown(self, tmp_path: Path) -> None:
        eng = _bare_pool_engine(tmp_path)
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterQuotaExhausted("daily quota", cooldown_key="openrouter_shared")
        )
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        handovers = [e for e in events if isinstance(e, HandoverEvent)]
        assert any(h.reason == "quota_exhausted" for h in handovers)
        assert eng.cooldowns.is_exhausted("openrouter_shared")

    async def test_quota_exhausted_skips_all_or_members(self, tmp_path: Path) -> None:
        """After quota exhaustion all OpenRouter pool members are skipped; floor answers."""
        eng = _bare_pool_engine(tmp_path)
        # Sonnet fails with quota exhaustion
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterQuotaExhausted("daily quota", cooldown_key="openrouter_shared")
        )
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]

        # All handovers should end at the floor (no intermediate pool stops)
        final_handover = [e for e in events if isinstance(e, HandoverEvent)][-1]
        assert final_handover.to_model == "gemma4:latest"

    async def test_successful_pool_member_marks_success(self, tmp_path: Path) -> None:
        eng = _bare_pool_engine(tmp_path)
        # Sonnet fails, frontier/a succeeds
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )
        await _consume(eng.complete_stream(_simple_request(), {}, "h.md"))
        assert not eng.model_health.is_dead("frontier/a:free")


# ---------------------------------------------------------------------------
# pool_health_status — tier annotation (Phase 3B model sidebar)
# ---------------------------------------------------------------------------


class TestPoolHealthStatus:
    def test_includes_tier_per_member(self, tmp_path: Path) -> None:
        eng = _bare_pool_engine(tmp_path)
        statuses = eng.pool_health_status()
        assert statuses is not None
        by_model = {s["model"]: s for s in statuses}
        assert by_model["frontier/a:free"]["tier"] == "frontier"
        assert by_model["frontier/b:free"]["tier"] == "frontier"
        assert by_model["strong/c:free"]["tier"] == "strong"
        assert by_model["reliable/d:free"]["tier"] == "reliable"

    def test_returns_none_for_legacy_schema2_config(self, tmp_path: Path) -> None:
        eng = _bare_engine(tmp_path, _two_rung_config())
        assert eng.pool_health_status() is None


# ---------------------------------------------------------------------------
# Pin behavior
# ---------------------------------------------------------------------------


class TestPoolPin:
    async def test_pin_honored_even_when_model_dead(self, tmp_path: Path) -> None:
        """A pinned model is tried first regardless of its health status."""
        eng = _bare_pool_engine(tmp_path)
        # Mark frontier/a dead, but pin it
        eng.model_health.mark_unavailable("frontier/a:free", "down")
        # frontier/a mock: succeeds (the "dead" mark was stale — model recovered)
        eng._pool_adapters["frontier/a:free"].complete_stream = _make_stream("pinned answer")

        thread_yaml = {"pinned_model": "frontier/a:free"}
        events = [e async for e in eng.complete_stream(_simple_request(), thread_yaml, "h.md")]

        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("pinned" in t for t in texts)
        assert not any(isinstance(e, HandoverEvent) for e in events)

    async def test_pin_failure_falls_through_to_health_order(
        self, tmp_path: Path
    ) -> None:
        """When the pinned model fails, cascade falls through to health-aware order."""
        eng = _bare_pool_engine(tmp_path)
        eng._pool_adapters["frontier/a:free"].complete_stream = _failing_stream(
            AdapterModelUnavailable("pinned model down")
        )
        # Mark Sonnet dead (pool path uses model slug as cooldown key, not rung key)
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "down")

        thread_yaml = {"pinned_model": "frontier/a:free"}
        events = [e async for e in eng.complete_stream(_simple_request(), thread_yaml, "h.md")]

        handovers = [e for e in events if isinstance(e, HandoverEvent)]
        assert len(handovers) >= 1
        # Should fall through to frontier/b (next live member)
        assert handovers[0].to_model == "frontier/b:free"


# ---------------------------------------------------------------------------
# Legacy regression
# ---------------------------------------------------------------------------


class TestLegacyRegression:
    async def test_schema2_config_uses_legacy_path(self, tmp_path: Path) -> None:
        """Schema-2 config must still use select_rung / legacy rung failover."""
        eng = _bare_engine(tmp_path, _two_rung_config())
        assert not eng._config.uses_pool

        async def _gen(req):  # type: ignore[no-untyped-def]
            yield Token(text="legacy answer", is_final=True)

        mock1 = _mock_adapter("anthropic")
        mock1.complete_stream = _gen
        eng._adapters[1] = mock1

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert "legacy answer" in texts

    async def test_schema2_failover_still_uses_rung_positions(
        self, tmp_path: Path
    ) -> None:
        """Schema-2 failover HandoverEvent uses integer rung positions (not pool idx)."""
        eng = _bare_engine(tmp_path, _two_rung_config())

        mock1 = _mock_adapter("anthropic")
        mock1.complete_stream = _failing_stream(AdapterUnavailable("down"))
        mock2 = _mock_adapter("openrouter")
        mock2.complete_stream = _make_stream("fallback")
        eng._adapters = {1: mock1, 2: mock2}

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        handovers = [e for e in events if isinstance(e, HandoverEvent)]
        assert len(handovers) == 1
        assert handovers[0].from_rung == 1
        assert handovers[0].to_rung == 2


# ---------------------------------------------------------------------------
# reasoning_mode threading
# ---------------------------------------------------------------------------


class TestReasoningModeThreading:
    def test_pool_candidate_carries_reasoning_mode(self, tmp_path: Path) -> None:
        """_pool_candidates() must expose reasoning_mode from the PoolMember."""
        cfg = CascadeConfig(
            schema_version=3,
            rungs=[RungConfig(position=1, adapter="anthropic", auth_mode="subscription",
                              models={"default": "claude-sonnet-4-6"})],
            pool=PoolConfig(
                adapter="openrouter",
                auth_mode="api_key_free_tier",
                tier_order=["frontier"],
                members=[
                    PoolMember(
                        model="nvidia/nemotron-3-super-120b-a12b:free",
                        tier="frontier",
                        reasoning_mode="exclude",
                    )
                ],
            ),
            floor=FloorConfig(adapter="ollama", auth_mode="local", model="gemma4:latest"),
        )
        eng = _bare_engine(tmp_path, cfg)
        nemotron_mock = _mock_adapter("openrouter")
        eng._pool_adapters = {"nvidia/nemotron-3-super-120b-a12b:free": nemotron_mock}
        eng._floor_adapter = _mock_adapter("ollama")

        candidates = eng._pool_candidates()
        nemotron = next(
            c for c in candidates
            if c.model == "nvidia/nemotron-3-super-120b-a12b:free"
        )
        assert nemotron.reasoning_mode == "exclude"


# ---------------------------------------------------------------------------
# Recovery probe (P2c-T05)
# ---------------------------------------------------------------------------

# A fixed past time we can use to create "expired backoff" entries in ModelHealth.
# Anything marked unavailable at T_PAST will have dead_until long expired by now.
T_PAST = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_healthy_status() -> HealthStatus:
    return HealthStatus(healthy=True, last_checked=datetime.now(UTC), detail="probe-ok")


def _make_unhealthy_status(detail: str = "Provider returned error") -> HealthStatus:
    return HealthStatus(healthy=False, last_checked=datetime.now(UTC), detail=detail)


class TestRecoveryProbe:
    def test_never_failed_model_needs_no_probe(self, tmp_path: Path) -> None:
        """A model with no failure history must never trigger a probe."""
        eng = _bare_pool_engine(tmp_path)
        candidates = eng._pool_candidates()
        frontier_a = next(c for c in candidates if c.model == "frontier/a:free")
        # No mark_unavailable called → consecutive_failures == 0
        assert not eng._needs_recovery_probe(frontier_a)

    def test_failed_model_in_backoff_needs_no_probe(self, tmp_path: Path) -> None:
        """A model still within its backoff window should not be probed (is_dead guards it)."""
        eng = _bare_pool_engine(tmp_path)
        # Mark unavailable right now so dead_until is in the future
        eng.model_health.mark_unavailable("frontier/a:free", "err")
        candidates = eng._pool_candidates()
        frontier_a = next(c for c in candidates if c.model == "frontier/a:free")
        # Still in backoff → _select_pool_candidate skips it; _needs_recovery_probe false
        assert not eng._needs_recovery_probe(frontier_a)

    def test_recovered_model_needs_probe(self, tmp_path: Path) -> None:
        """consecutive_failures > 0, backoff expired → recovery edge → probe needed."""
        eng = _bare_pool_engine(tmp_path)
        # Mark failure at a past time so the 5-min backoff has long expired
        eng.model_health.mark_unavailable("frontier/a:free", "err", now=T_PAST)
        candidates = eng._pool_candidates()
        frontier_a = next(c for c in candidates if c.model == "frontier/a:free")
        assert eng._needs_recovery_probe(frontier_a)

    def test_sonnet_never_probed(self, tmp_path: Path) -> None:
        """Sonnet (no quota_key) must never trigger a probe regardless of health state."""
        eng = _bare_pool_engine(tmp_path)
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "err", now=T_PAST)
        candidates = eng._pool_candidates()
        sonnet = next(c for c in candidates if c.model == "claude-sonnet-4-6")
        assert not eng._needs_recovery_probe(sonnet)

    async def test_probe_healthy_selects_candidate_and_marks_success(
        self, tmp_path: Path
    ) -> None:
        """Probe healthy → mark_success, candidate returned."""
        eng = _bare_pool_engine(tmp_path)
        # frontier/a: failure at T_PAST so backoff has expired (recovery edge)
        eng.model_health.mark_unavailable("frontier/a:free", "err", now=T_PAST)
        # Sonnet: mark dead right now so _select_pool_candidate skips it
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "dead")

        eng._pool_adapters["frontier/a:free"].health_check = AsyncMock(
            return_value=_make_healthy_status()
        )

        candidates = eng._pool_candidates()
        result = await eng._select_with_recovery_probe(candidates, set())

        assert result is not None
        assert result.model == "frontier/a:free"
        # mark_success resets failures
        entry = eng.model_health._data.get("frontier/a:free")
        assert entry is not None
        assert entry.consecutive_failures == 0

    async def test_probe_unhealthy_skips_to_next_candidate(
        self, tmp_path: Path
    ) -> None:
        """Probe unhealthy → mark_unavailable, next candidate selected."""
        eng = _bare_pool_engine(tmp_path)
        eng.model_health.mark_unavailable("frontier/a:free", "err", now=T_PAST)
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "dead")

        eng._pool_adapters["frontier/a:free"].health_check = AsyncMock(
            return_value=_make_unhealthy_status("still down")
        )

        candidates = eng._pool_candidates()
        tried: set[str] = set()
        result = await eng._select_with_recovery_probe(candidates, tried)

        assert result is not None
        assert result.model == "frontier/b:free"  # skipped a, landed on b
        assert "frontier/a:free" in tried
        # frontier/a is dead again
        assert eng.model_health.is_dead("frontier/a:free")

    async def test_recovery_probe_false_skips_probe(self, tmp_path: Path) -> None:
        """recovery_probe: false → candidate selected without probing, even on recovery edge."""
        members = [
            PoolMember(model="frontier/a:free", tier="frontier"),
            PoolMember(model="frontier/b:free", tier="frontier"),
        ]
        cfg = CascadeConfig(
            schema_version=3,
            rungs=[RungConfig(position=1, adapter="anthropic", auth_mode="subscription",
                              models={"default": "claude-sonnet-4-6"})],
            pool=PoolConfig(
                adapter="openrouter",
                auth_mode="api_key_free_tier",
                tier_order=["frontier"],
                members=members,
                recovery_probe=False,
            ),
            floor=FloorConfig(adapter="ollama", auth_mode="local", model="gemma4:latest"),
        )
        eng = _bare_pool_engine(tmp_path, cfg)
        eng.model_health.mark_unavailable("frontier/a:free", "err", now=T_PAST)
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "dead")

        mock_hc = AsyncMock(return_value=_make_healthy_status())
        eng._pool_adapters["frontier/a:free"].health_check = mock_hc

        candidates = eng._pool_candidates()
        result = await eng._select_with_recovery_probe(candidates, set())

        assert result is not None
        assert result.model == "frontier/a:free"
        mock_hc.assert_not_called()  # probe must NOT have fired

    async def test_probe_cached_prevents_second_probe(self, tmp_path: Path) -> None:
        """Within the TTL window, a second recovery selection must not re-probe."""
        eng = _bare_pool_engine(tmp_path)
        eng.model_health.mark_unavailable("frontier/a:free", "err", now=T_PAST)
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "dead")

        # Pre-fill the probe cache as if a probe already ran
        eng._probe_cache["frontier/a:free"] = datetime.now(UTC) + timedelta(seconds=60)

        mock_hc = AsyncMock(return_value=_make_healthy_status())
        eng._pool_adapters["frontier/a:free"].health_check = mock_hc

        candidates = eng._pool_candidates()
        result = await eng._select_with_recovery_probe(candidates, set())

        assert result is not None
        assert result.model == "frontier/a:free"
        mock_hc.assert_not_called()  # cache hit — no probe

    async def test_probe_fires_through_complete_stream(self, tmp_path: Path) -> None:
        """End-to-end: recovering model gets probed; on healthy, its stream is used."""
        eng = _bare_pool_engine(tmp_path)
        eng.model_health.mark_unavailable("frontier/a:free", "err", now=T_PAST)
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "dead")

        eng._pool_adapters["frontier/a:free"].health_check = AsyncMock(
            return_value=_make_healthy_status()
        )
        eng._pool_adapters["frontier/a:free"].complete_stream = _make_stream("probed reply")

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("probed reply" in t for t in texts)
        # No handover — the probe is transparent to the streaming turn
        assert not any(isinstance(e, HandoverEvent) for e in events)

    async def test_probe_unhealthy_through_complete_stream_cascades(
        self, tmp_path: Path
    ) -> None:
        """Unhealthy probe through complete_stream cascades to next model."""
        eng = _bare_pool_engine(tmp_path)
        eng.model_health.mark_unavailable("frontier/a:free", "err", now=T_PAST)
        eng.model_health.mark_unavailable("claude-sonnet-4-6", "dead")

        eng._pool_adapters["frontier/a:free"].health_check = AsyncMock(
            return_value=_make_unhealthy_status("still down")
        )

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        # frontier/a was probed and failed, so frontier/b should answer
        assert any("frontier/b" in t for t in texts)


# ---------------------------------------------------------------------------
# Task classifier integration (P2c-T07)
# ---------------------------------------------------------------------------


def _pool_config_with_roles(
    coding_model: str = "frontier/coding:free",
    general_model: str = "frontier/general:free",
) -> CascadeConfig:
    """Pool config where general-role model is declared FIRST, coding-role model second.

    This ordering means role bias is detectable: without bias the general model wins;
    with route_by_role=True on a coding turn the coding model is sorted to the front.
    """
    return CascadeConfig(
        schema_version=3,
        rungs=[
            RungConfig(
                position=1,
                adapter="anthropic",
                auth_mode="subscription",
                models={"default": "claude-sonnet-4-6"},
            )
        ],
        pool=PoolConfig(
            adapter="openrouter",
            auth_mode="api_key_free_tier",
            tier_order=["frontier", "strong", "reliable"],
            members=[
                PoolMember(model=general_model, tier="frontier", roles=["general"]),
                PoolMember(model=coding_model, tier="frontier", roles=["coding"]),
            ],
        ),
        floor=FloorConfig(adapter="ollama", auth_mode="local", model="gemma4:latest"),
    )


def _coding_request() -> NexusRequest:
    return NexusRequest(
        thread_id="classifier-test-thread",
        messages=[
            NexusMessage(role="system", content="You are helpful."),
            NexusMessage(role="user", content="```python\ndef foo():\n    pass\n```\nFix the bug."),
        ],
        style_profile=StyleProfile(),
    )


class TestClassificationEventEmission:
    async def test_classification_event_emitted_when_enabled(
        self, tmp_path: Path
    ) -> None:
        """Pool path emits ClassificationEvent when classifier.enabled=True (default)."""
        from orchestrator.cascade.events import ClassificationEvent

        eng = _bare_pool_engine(tmp_path)
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        cls_events = [e for e in events if isinstance(e, ClassificationEvent)]
        assert len(cls_events) == 1
        assert cls_events[0].task_class in ("conversational", "research", "coding", "general")
        assert cls_events[0].backend == "heuristic"

    async def test_explicit_hard_mode_keeps_sonnet_first(
        self, tmp_path: Path
    ) -> None:
        """With an explicit hard mode, ClassificationEvent is emitted but the
        selection order stays Sonnet-first regardless of task class."""
        from orchestrator.cascade.events import ClassificationEvent

        eng = _bare_pool_engine(tmp_path)
        events = [
            e async for e in eng.complete_stream(_simple_request(mode="hard"), {}, "h.md")
        ]

        cls_events = [e for e in events if isinstance(e, ClassificationEvent)]
        token_events = [e for e in events if isinstance(e, TokenEvent)]
        assert len(cls_events) == 1
        assert cls_events[0].mode == "hard"
        assert any(e.token.text == "sonnet answer" for e in token_events)

    async def test_no_classification_event_when_classifier_disabled(
        self, tmp_path: Path
    ) -> None:
        """When classifier.enabled=False, no ClassificationEvent is yielded."""
        from orchestrator.cascade.events import ClassificationEvent
        from orchestrator.types import ClassifierConfig

        cfg = _pool_config()
        cfg = cfg.model_copy(update={"classifier": ClassifierConfig(enabled=False)})
        eng = _bare_pool_engine(tmp_path, cfg)
        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        cls_events = [e for e in events if isinstance(e, ClassificationEvent)]
        assert len(cls_events) == 0

    async def test_short_message_classified_conversational(
        self, tmp_path: Path
    ) -> None:
        from orchestrator.cascade.events import ClassificationEvent

        eng = _bare_pool_engine(tmp_path)
        req = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="Hi!")],
            style_profile=StyleProfile(),
        )
        events = [e async for e in eng.complete_stream(req, {}, "h.md")]
        cls_events = [e for e in events if isinstance(e, ClassificationEvent)]
        assert cls_events[0].task_class == "conversational"

    async def test_coding_message_classified_coding(
        self, tmp_path: Path
    ) -> None:
        from orchestrator.cascade.events import ClassificationEvent

        eng = _bare_pool_engine(tmp_path)
        events = [e async for e in eng.complete_stream(_coding_request(), {}, "h.md")]
        cls_events = [e for e in events if isinstance(e, ClassificationEvent)]
        assert cls_events[0].task_class == "coding"

    async def test_rung1_sonnet_path_also_emits_classification_event(
        self, tmp_path: Path
    ) -> None:
        """Schema-2 (legacy rung-only) config must also emit a ClassificationEvent.

        This is the core fix for the T07 wiring bug: classification must run on
        ALL paths, not just when pool routing is active.
        """
        from orchestrator.cascade.events import ClassificationEvent

        # Use a schema-2 config (no pool block) — routes through _complete_stream_legacy
        eng = _bare_engine(tmp_path, _two_rung_config())
        assert not eng._config.uses_pool

        async def _gen(req):  # type: ignore[no-untyped-def]
            yield Token(text="sonnet reply", is_final=True)

        mock1 = _mock_adapter("anthropic")
        mock1.complete_stream = _gen
        eng._adapters[1] = mock1

        events = [e async for e in eng.complete_stream(_simple_request(), {}, "h.md")]
        cls_events = [e for e in events if isinstance(e, ClassificationEvent)]
        token_events = [e for e in events if isinstance(e, TokenEvent)]

        # Exactly one ClassificationEvent must be emitted even on the legacy path
        assert len(cls_events) == 1
        assert cls_events[0].task_class in ("conversational", "research", "coding", "general")
        assert cls_events[0].backend == "heuristic"
        # The turn still completes normally
        assert any(e.token.text == "sonnet reply" for e in token_events)


class TestRoleBasedSelection:
    async def test_coding_turn_prefers_coding_role_in_pool(
        self, tmp_path: Path
    ) -> None:
        """Auto mode maps a coding turn to hard with coding-role bias: the
        coding-role pool member is tried before the general-role one."""
        cfg = _pool_config_with_roles()
        eng = _bare_pool_engine(tmp_path, cfg)

        # Sonnet fails → pool entered; coding-role model should answer
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )
        eng._pool_adapters["frontier/coding:free"].complete_stream = _make_stream(
            "coding model answer"
        )
        eng._pool_adapters["frontier/general:free"].complete_stream = _make_stream(
            "general model answer"
        )

        events = [e async for e in eng.complete_stream(_coding_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("coding model" in t for t in texts)
        assert not any("general model" in t for t in texts)

    async def test_falls_back_to_general_when_all_coding_role_dead(
        self, tmp_path: Path
    ) -> None:
        """Health wins: if the coding-role model is dead, the general-role model answers."""
        cfg = _pool_config_with_roles()
        eng = _bare_pool_engine(tmp_path, cfg)

        # Sonnet and coding-role model are dead
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )
        eng.model_health.mark_unavailable("frontier/coding:free", "down")
        eng._pool_adapters["frontier/general:free"].complete_stream = _make_stream(
            "general model fallback"
        )

        events = [e async for e in eng.complete_stream(_coding_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("general model fallback" in t for t in texts)

    async def test_declared_order_preserved_on_non_coding_turn(
        self, tmp_path: Path
    ) -> None:
        """Coding bias only applies to coding turns.

        general-role model is declared first in the config, so it wins when the
        turn is NOT a coding turn — declared tier order is preserved.
        """
        cfg = _pool_config_with_roles()
        eng = _bare_pool_engine(tmp_path, cfg)

        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )
        eng._pool_adapters["frontier/general:free"].complete_stream = _make_stream(
            "general model first"
        )
        eng._pool_adapters["frontier/coding:free"].complete_stream = _make_stream(
            "coding model second"
        )

        # Non-coding turn in hard mode → declared order, no coding bias
        events = [
            e async for e in eng.complete_stream(_simple_request(mode="hard"), {}, "h.md")
        ]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("general model first" in t for t in texts)
        assert not any("coding model second" in t for t in texts)


# ---------------------------------------------------------------------------
# Mode policy matrix (Phase 3W)
# ---------------------------------------------------------------------------


def _tiered_roles_config() -> CascadeConfig:
    """Pool covering all tiers/roles so mode ordering is observable."""
    return CascadeConfig(
        schema_version=3,
        rungs=[
            RungConfig(
                position=1,
                adapter="anthropic",
                auth_mode="subscription",
                models={"default": "claude-sonnet-4-6"},
            )
        ],
        pool=PoolConfig(
            adapter="openrouter",
            auth_mode="api_key_free_tier",
            tier_order=["frontier", "strong", "reliable"],
            members=[
                PoolMember(model="frontier/reason:free", tier="frontier",
                           roles=["reasoning", "general"]),
                PoolMember(model="strong/code:free", tier="strong", roles=["coding"]),
                PoolMember(model="reliable/slowgen:free", tier="reliable",
                           roles=["coding"]),
                PoolMember(model="reliable/fast:free", tier="reliable",
                           roles=["general", "fast"]),
            ],
        ),
        floor=FloorConfig(adapter="ollama", auth_mode="local", model="gemma4:latest"),
    )


class TestModePolicy:
    def _engine(self, tmp_path: Path) -> CascadeEngine:
        return _bare_pool_engine(tmp_path, _tiered_roles_config())

    def test_hard_order_sonnet_then_tiers_then_floor(self, tmp_path: Path) -> None:
        eng = self._engine(tmp_path)
        order = [c.model for c in eng._pool_candidates(mode="hard")]
        assert order == [
            "claude-sonnet-4-6",
            "frontier/reason:free",
            "strong/code:free",
            "reliable/slowgen:free",
            "reliable/fast:free",
            "gemma4:latest",
        ]

    def test_light_order_skips_rung1_reversed_tiers_fast_first(
        self, tmp_path: Path
    ) -> None:
        eng = self._engine(tmp_path)
        order = [c.model for c in eng._pool_candidates(mode="light")]
        # No Sonnet; reliable first with fast/general-role member ahead of the
        # coding-only one; then strong, frontier; floor last.
        assert order == [
            "reliable/fast:free",
            "reliable/slowgen:free",
            "strong/code:free",
            "frontier/reason:free",
            "gemma4:latest",
        ]
        assert "claude-sonnet-4-6" not in order

    def test_research_order_rung1_then_reasoning_first(self, tmp_path: Path) -> None:
        eng = self._engine(tmp_path)
        order = [c.model for c in eng._pool_candidates(mode="research")]
        assert order[0] == "claude-sonnet-4-6"
        assert order[1] == "frontier/reason:free"  # reasoning role bubbles up
        assert order[-1] == "gemma4:latest"

    def test_hard_coding_bias_reorders_pool(self, tmp_path: Path) -> None:
        eng = self._engine(tmp_path)
        order = [c.model for c in eng._pool_candidates(task_class="coding", mode="hard")]
        assert order[0] == "claude-sonnet-4-6"
        # coding-role members ahead of non-coding pool members, floor still last
        assert order[1] == "strong/code:free"
        assert order[-1] == "gemma4:latest"

    async def test_auto_conversational_routes_light(self, tmp_path: Path) -> None:
        """'hi' in auto mode → light → a pool member answers, never Sonnet."""
        eng = self._engine(tmp_path)
        req = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="hi")],
            style_profile=StyleProfile(),
        )
        events = [e async for e in eng.complete_stream(req, {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("reliable/fast" in t for t in texts)
        assert not any("sonnet" in t for t in texts)

    async def test_auto_coding_routes_hard_sonnet_first(self, tmp_path: Path) -> None:
        eng = self._engine(tmp_path)
        events = [e async for e in eng.complete_stream(_coding_request(), {}, "h.md")]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("sonnet answer" in t for t in texts)

    async def test_light_falls_back_by_health_then_floor(self, tmp_path: Path) -> None:
        """Health beats mode preference: dead light-preferred members are skipped."""
        eng = self._engine(tmp_path)
        for m in ("reliable/fast:free", "reliable/slowgen:free", "strong/code:free"):
            eng.model_health.mark_unavailable(m, "down")
        events = [
            e async for e in eng.complete_stream(_simple_request(mode="light"), {}, "h.md")
        ]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("frontier/reason" in t for t in texts)

    async def test_pin_beats_mode_light_still_answers_with_sonnet(
        self, tmp_path: Path
    ) -> None:
        """Explicit pin wins even in light mode, where rung-1 is out of the order."""
        eng = self._engine(tmp_path)
        events = [
            e
            async for e in eng.complete_stream(
                _simple_request(mode="light"),
                {"pinned_model": "claude-sonnet-4-6"},
                "h.md",
            )
        ]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert any("sonnet answer" in t for t in texts)

    async def test_research_fallback_prepends_notice(self, tmp_path: Path) -> None:
        """Research mode with rung-1 down: reasoning member answers, notice first."""
        from orchestrator.prompts import RESEARCH_FALLBACK_NOTICE

        eng = self._engine(tmp_path)
        eng._adapters[1].complete_stream = _failing_stream(
            AdapterModelUnavailable("sonnet down")
        )
        events = [
            e
            async for e in eng.complete_stream(
                _simple_request(mode="research"), {}, "h.md"
            )
        ]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert texts[0] == RESEARCH_FALLBACK_NOTICE
        assert any("frontier/reason" in t for t in texts)

    async def test_research_via_rung1_has_no_notice(self, tmp_path: Path) -> None:
        from orchestrator.prompts import RESEARCH_FALLBACK_NOTICE

        eng = self._engine(tmp_path)
        events = [
            e
            async for e in eng.complete_stream(
                _simple_request(mode="research"), {}, "h.md"
            )
        ]
        texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
        assert RESEARCH_FALLBACK_NOTICE not in texts
        assert any("sonnet answer" in t for t in texts)

    async def test_adapter_sees_resolved_mode(self, tmp_path: Path) -> None:
        """request.mode is rewritten to the resolved mode before the adapter call."""
        eng = self._engine(tmp_path)
        seen_modes: list[str] = []

        async def _capture(req):  # type: ignore[no-untyped-def]
            seen_modes.append(req.mode)
            yield Token(text="pool answer", is_final=True)

        eng._pool_adapters["reliable/fast:free"].complete_stream = _capture
        req = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="hi")],
            style_profile=StyleProfile(),
        )
        await _consume(eng.complete_stream(req, {}, "h.md"))
        assert seen_modes == ["light"]

    async def test_meta_event_emitted_with_mode_and_model(self, tmp_path: Path) -> None:
        eng = self._engine(tmp_path)
        req = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="hi")],
            style_profile=StyleProfile(),
        )
        events = [e async for e in eng.complete_stream(req, {}, "h.md")]
        metas = [e for e in events if isinstance(e, MetaEvent)]
        assert len(metas) == 1
        assert metas[0].mode == "light"
        assert metas[0].requested_mode == "auto"
        assert metas[0].model == "reliable/fast:free"

    async def test_classification_event_carries_resolved_mode(
        self, tmp_path: Path
    ) -> None:
        from orchestrator.cascade.events import ClassificationEvent

        eng = self._engine(tmp_path)
        req = NexusRequest(
            thread_id="t",
            messages=[NexusMessage(role="user", content="hi")],
            style_profile=StyleProfile(),
        )
        events = [e async for e in eng.complete_stream(req, {}, "h.md")]
        cls = next(e for e in events if isinstance(e, ClassificationEvent))
        assert cls.task_class == "conversational"
        assert cls.mode == "light"

    async def test_opus_never_selected_by_any_mode(self, tmp_path: Path) -> None:
        """No mode ordering ever includes the premium (Opus) model."""
        eng = self._engine(tmp_path)
        for mode in ("light", "hard", "research", "auto"):
            models = [c.model for c in eng._pool_candidates(mode=mode)]
            assert not any("opus" in m for m in models), f"mode={mode} exposed Opus"


# ---------------------------------------------------------------------------
# Async helper
# ---------------------------------------------------------------------------


async def _consume(gen: AsyncGenerator) -> list:  # type: ignore[type-arg]
    return [e async for e in gen]
