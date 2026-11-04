"""Tests for orchestrator/orchestrator.py — CascadeEngine is mocked."""
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.cascade.events import (
    ErrorEvent,
    HandoverEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from orchestrator.orchestrator import Orchestrator
from orchestrator.projects.types import ProjectCandidate, ProjectNote
from orchestrator.types import HealthStatus, NexusRequest, Token

_CASCADE_YAML = """\
schema_version: 2
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
    use_ruflo: false
    models:
      default: claude-sonnet-4-6
"""


# ---------------------------------------------------------------------------
# Mock cascade engine
# ---------------------------------------------------------------------------


class _MockCascade:
    """A CascadeEngine substitute that yields configurable events."""

    def __init__(self, events_to_yield: list | None = None) -> None:
        self._events = events_to_yield or [
            TokenEvent(token=Token(text="pong", is_final=False)),
            TokenEvent(token=Token(text="", is_final=True)),
        ]

    def select_rung(self, thread_yaml: dict) -> int:  # type: ignore[type-arg]
        return 1

    def _active_model_for_rung(self, position: int) -> str:
        return "claude-sonnet-4-6"

    async def complete_stream(
        self,
        request: NexusRequest,
        thread_yaml: dict,  # type: ignore[type-arg]
        live_history_filename: str,
    ) -> AsyncGenerator[Any, None]:
        for event in self._events:
            yield event

    async def health_check_all(self) -> dict[int, HealthStatus]:
        return {1: HealthStatus(healthy=True, last_checked=datetime.now(UTC))}

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    config_dir = tmp_path / "Orchestrator" / "Config"
    config_dir.mkdir(parents=True)
    (config_dir / "cascade.yaml").write_text(_CASCADE_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def orch(vault: Path) -> Orchestrator:
    with patch("orchestrator.orchestrator.CascadeEngine", return_value=_MockCascade()):
        return Orchestrator(vault)


def _orch_with_events(vault: Path, events: list) -> Orchestrator:
    with patch(
        "orchestrator.orchestrator.CascadeEngine", return_value=_MockCascade(events)
    ):
        return Orchestrator(vault)


# ---------------------------------------------------------------------------
# New thread + basic streaming
# ---------------------------------------------------------------------------


class TestNewThread:
    async def test_creates_jsonl_and_projection_after_turn(
        self, orch: Orchestrator, vault: Path
    ) -> None:
        thread_id = orch.new_thread_id()
        # save-worthiness filter (P3B) bypassed: this test is about JSONL/
        # projection mechanics, not the filter — that has its own test class.
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            emitted = [e async for e in orch.send_turn_stream(thread_id, "ping")]

        id_short = thread_id.replace("-", "")[:16]
        jsonl = vault / "Orchestrator" / "Logs" / f"{id_short}.jsonl"
        assert jsonl.exists(), "JSONL log not created"

        live_files = list((vault / "Orchestrator" / "Live").glob("*.md"))
        assert len(live_files) == 1, "Expected one markdown projection"

        events = orch.load_thread(thread_id)
        assert len(events) == 2
        assert events[0]["event_type"] == "turn_user"
        assert events[1]["event_type"] == "turn_assistant"

        text_events = [e for e in emitted if e.get("type") == "token" and e["text"]]
        assert len(text_events) == 1
        assert text_events[0]["text"] == "pong"

    async def test_second_turn_appends_to_same_jsonl(
        self, orch: Orchestrator, vault: Path
    ) -> None:
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in orch.send_turn_stream(thread_id, "first"):
                pass
            async for _ in orch.send_turn_stream(thread_id, "second"):
                pass

        id_short = thread_id.replace("-", "")[:16]
        jsonl = vault / "Orchestrator" / "Logs" / f"{id_short}.jsonl"
        events = orch.load_thread(thread_id)
        assert len(events) == 4  # user + assistant + user + assistant
        assert jsonl.exists()
        assert len(list((vault / "Orchestrator" / "Logs").glob("*.jsonl"))) == 1


# ---------------------------------------------------------------------------
# model_id fix: active_model reflects actual rung that answered
# ---------------------------------------------------------------------------


class TestModelIdTracking:
    async def test_no_handover_records_rung1_model(
        self, orch: Orchestrator, vault: Path
    ) -> None:
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in orch.send_turn_stream(thread_id, "hello"):
                pass
        events = orch.load_thread(thread_id)
        assistant_evt = next(e for e in events if e["event_type"] == "turn_assistant")
        assert assistant_evt["payload"]["model_id"] == "claude-sonnet-4-6"

    async def test_handover_updates_model_id_to_rung2(
        self, vault: Path
    ) -> None:

        handover_path = vault / "Orchestrator" / "Handovers" / "dummy_handover_01.md"
        handover_path.parent.mkdir(parents=True, exist_ok=True)
        handover_path.write_text(
            "---\ntype: handover\nfrom_model: claude-sonnet-4-6\nto_model: minimax/minimax-m1\ntrigger: HARD_FAIL\ntrigger_detail: down\n---\n\nbody",  # noqa: E501
            encoding="utf-8",
        )

        events_sequence = [
            TokenEvent(token=Token(text="err", is_final=False)),
            HandoverEvent(
                from_rung=1,
                to_rung=2,
                from_model="claude-sonnet-4-6",
                to_model="minimax/minimax-m1",
                handover_path=handover_path,
                trigger="HARD_FAIL",
            ),
            TokenEvent(token=Token(text="ok", is_final=False)),
            TokenEvent(token=Token(text="", is_final=True)),
        ]
        o = _orch_with_events(vault, events_sequence)
        thread_id = o.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in o.send_turn_stream(thread_id, "hello"):
                pass

        log_events = o.load_thread(thread_id)
        assistant_evt = next(e for e in log_events if e["event_type"] == "turn_assistant")
        assert assistant_evt["payload"]["model_id"] == "minimax/minimax-m1"
        assert assistant_evt["payload"]["auth_mode"] == "api_key_free_tier"


# ---------------------------------------------------------------------------
# HandoverEvent in stream
# ---------------------------------------------------------------------------


class TestHandoverEvent:
    async def test_handover_written_to_jsonl(self, vault: Path) -> None:
        handover_path = vault / "Orchestrator" / "Handovers" / "dummy_handover_01.md"
        handover_path.parent.mkdir(parents=True, exist_ok=True)
        handover_path.write_text(
            "---\ntype: handover\nfrom_model: a\nto_model: b\ntrigger: HARD_FAIL\ntrigger_detail: d\n---\n\nbody",  # noqa: E501
            encoding="utf-8",
        )

        events_sequence = [
            HandoverEvent(
                from_rung=1,
                to_rung=2,
                from_model="claude-sonnet-4-6",
                to_model="minimax/minimax-m1",
                handover_path=handover_path,
                trigger="HARD_FAIL",
            ),
            TokenEvent(token=Token(text="reply", is_final=False)),
            TokenEvent(token=Token(text="", is_final=True)),
        ]
        o = _orch_with_events(vault, events_sequence)
        thread_id = o.new_thread_id()
        stream_events = [e async for e in o.send_turn_stream(thread_id, "hi")]

        # SSE stream contains a handover dict
        handover_dicts = [e for e in stream_events if e.get("type") == "handover"]
        assert len(handover_dicts) == 1
        assert handover_dicts[0]["from_rung"] == 1
        assert handover_dicts[0]["to_rung"] == 2
        assert handover_dicts[0]["trigger"] == "HARD_FAIL"

        # JSONL log contains a 'handover' event
        log_events = o.load_thread(thread_id)
        handover_log = [e for e in log_events if e["event_type"] == "handover"]
        assert len(handover_log) == 1
        assert handover_log[0]["payload"]["from_model"] == "claude-sonnet-4-6"
        assert handover_log[0]["payload"]["to_model"] == "minimax/minimax-m1"


# ---------------------------------------------------------------------------
# Terminal ErrorEvent aborts without writing turn_assistant
# ---------------------------------------------------------------------------


class TestTerminalError:
    async def test_terminal_error_skips_turn_assistant(self, vault: Path) -> None:
        events_sequence = [
            ErrorEvent(message="all rungs exhausted", terminal=True),
        ]
        o = _orch_with_events(vault, events_sequence)
        thread_id = o.new_thread_id()
        stream_events = [e async for e in o.send_turn_stream(thread_id, "hi")]

        error_dicts = [e for e in stream_events if e.get("type") == "error"]
        assert len(error_dicts) == 1
        assert error_dicts[0]["terminal"] is True

        log_events = o.load_thread(thread_id)
        # Neither turn_user nor turn_assistant — the turn aborted before the
        # end-of-turn save block runs at all (P3B moved turn_user there too).
        assert log_events == []

    async def test_non_terminal_error_continues_then_writes_assistant(
        self, vault: Path
    ) -> None:
        events_sequence = [
            ErrorEvent(message="soft transient error", terminal=False),
            TokenEvent(token=Token(text="ok", is_final=False)),
            TokenEvent(token=Token(text="", is_final=True)),
        ]
        o = _orch_with_events(vault, events_sequence)
        thread_id = o.new_thread_id()
        # save-worthiness filter (P3B) bypassed: this test is about the
        # non-terminal-error-continues mechanic, not the filter itself.
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            stream_events = [e async for e in o.send_turn_stream(thread_id, "hi")]

        # A non-terminal error is emitted but the loop continues
        error_dicts = [e for e in stream_events if e.get("type") == "error"]
        assert len(error_dicts) == 1
        assert error_dicts[0]["terminal"] is False

        log_events = o.load_thread(thread_id)
        assert any(e["event_type"] == "turn_assistant" for e in log_events)


# ---------------------------------------------------------------------------
# _load_thread_yaml — reads projected markdown frontmatter
# ---------------------------------------------------------------------------


class TestLoadThreadYaml:
    async def test_returns_none_for_new_thread(self, orch: Orchestrator) -> None:
        assert orch._load_thread_yaml("aaaa-bbbb-cccc-dddd-eeeeeeeeeee0") is None

    async def test_reads_frontmatter_from_projected_file(
        self, orch: Orchestrator, vault: Path
    ) -> None:
        thread_id = "aaaa0000-0000-7000-8000-000000000001"
        id_short = thread_id.replace("-", "")[:8]
        live_dir = vault / "Orchestrator" / "Live"
        live_dir.mkdir(parents=True, exist_ok=True)
        fake_md = live_dir / f"2026-05-18_thread-{id_short}_hello.md"
        fake_md.write_text(
            "---\nthread_id: aaaa0000-0000-7000-8000-000000000001\ntitle: hello\npinned_model: claude-opus-4-7\n---\n\nbody",  # noqa: E501
            encoding="utf-8",
        )
        result = orch._load_thread_yaml(thread_id)
        assert result is not None
        assert result["pinned_model"] == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# load_thread / list_threads
# ---------------------------------------------------------------------------


class TestLoadThread:
    async def test_returns_events_in_order(self, orch: Orchestrator) -> None:
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in orch.send_turn_stream(thread_id, "hello"):
                pass
        events = orch.load_thread(thread_id)
        assert events[0]["event_type"] == "turn_user"
        assert events[1]["event_type"] == "turn_assistant"
        assert events[1]["payload"]["content"] == "pong"


class TestListThreads:
    async def test_returns_one_entry_after_one_thread(self, orch: Orchestrator) -> None:
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in orch.send_turn_stream(thread_id, "hi"):
                pass
        threads = orch.list_threads()
        assert len(threads) == 1
        assert threads[0]["thread_id"] == thread_id

    async def test_returns_multiple_entries(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            for msg in ("alpha", "beta"):
                tid = orch.new_thread_id()
                async for _ in orch.send_turn_stream(tid, msg):
                    pass
        assert len(orch.list_threads()) == 2


# ---------------------------------------------------------------------------
# pin_thread / unpin_thread
# ---------------------------------------------------------------------------


class TestPinThread:
    async def test_pin_thread_sets_frontmatter_pinned_model(
        self, orch: Orchestrator, vault: Path
    ) -> None:
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in orch.send_turn_stream(thread_id, "hello"):
                pass
        orch.pin_thread(thread_id, "claude-opus-4-7")

        result = orch._load_thread_yaml(thread_id)
        assert result is not None
        assert result.get("pinned_model") == "claude-opus-4-7"

    async def test_unpin_thread_clears_pinned_model(
        self, orch: Orchestrator, vault: Path
    ) -> None:
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in orch.send_turn_stream(thread_id, "hello"):
                pass
        orch.pin_thread(thread_id, "claude-opus-4-7")
        orch.unpin_thread(thread_id)

        result = orch._load_thread_yaml(thread_id)
        assert result is not None
        assert result.get("pinned_model") is None

    async def test_pin_thread_appends_thread_pin_event(
        self, orch: Orchestrator, vault: Path
    ) -> None:
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            async for _ in orch.send_turn_stream(thread_id, "hello"):
                pass
        orch.pin_thread(thread_id, "minimax/minimax-m2.5")

        events = orch.load_thread(thread_id)
        pin_events = [e for e in events if e["event_type"] == "thread_pin"]
        assert len(pin_events) == 1
        assert pin_events[0]["payload"]["model"] == "minimax/minimax-m2.5"

    def test_pin_thread_raises_for_unknown_thread(self, orch: Orchestrator) -> None:
        with pytest.raises(ValueError, match="not found"):
            orch.pin_thread("00000000-0000-7000-8000-000000000000", "claude-opus-4-7")

    def test_unpin_thread_raises_for_unknown_thread(self, orch: Orchestrator) -> None:
        with pytest.raises(ValueError, match="not found"):
            orch.unpin_thread("00000000-0000-7000-8000-000000000000")


# ---------------------------------------------------------------------------
# _auth_mode_for_model
# ---------------------------------------------------------------------------


class TestAuthModeForModel:
    def test_claude_model_is_subscription(self, orch: Orchestrator) -> None:
        assert orch._auth_mode_for_model("claude-sonnet-4-6") == "subscription"
        assert orch._auth_mode_for_model("claude-opus-4-7") == "subscription"

    def test_slash_model_is_api_key_free_tier(self, orch: Orchestrator) -> None:
        assert orch._auth_mode_for_model("minimax/minimax-m1") == "api_key_free_tier"
        assert orch._auth_mode_for_model("x-ai/grok-3-mini-beta") == "api_key_free_tier"

    def test_local_model_is_local(self, orch: Orchestrator) -> None:
        assert orch._auth_mode_for_model("gemma4:latest") == "local"
        assert orch._auth_mode_for_model("ollama") == "local"


# ---------------------------------------------------------------------------
# Tool-call event pass-through (P2b-T05)
# ---------------------------------------------------------------------------


class TestToolCallPassThrough:
    async def test_tool_call_event_yielded_as_dict(self, vault: Path) -> None:
        """ToolCallEvent from the cascade engine becomes a tool_call SSE dict."""
        events = [
            ToolCallEvent(name="web_search", input={"query": "weather"}),
            TokenEvent(token=Token(text="It is sunny.", is_final=True)),
        ]
        orch = _orch_with_events(vault, events)
        thread_id = orch.new_thread_id()
        emitted = [e async for e in orch.send_turn_stream(thread_id, "hi")]

        tool_calls = [e for e in emitted if e.get("type") == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "web_search"
        assert tool_calls[0]["input"] == {"query": "weather"}

    async def test_tool_result_event_yielded_as_dict(self, vault: Path) -> None:
        """ToolResultEvent becomes a tool_result SSE dict."""
        events = [
            ToolResultEvent(name="web_search", content="[1] Result\nhttps://x.com\nsnippet"),
            TokenEvent(token=Token(text="Answer.", is_final=True)),
        ]
        orch = _orch_with_events(vault, events)
        thread_id = orch.new_thread_id()
        emitted = [e async for e in orch.send_turn_stream(thread_id, "hi")]

        tool_results = [e for e in emitted if e.get("type") == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["name"] == "web_search"
        assert "Result" in tool_results[0]["content"]

    async def test_tool_call_tokens_excluded_from_assistant_content(
        self, vault: Path
    ) -> None:
        """tool_call / tool_result events must not be logged as assistant content."""
        events = [
            ToolCallEvent(name="web_search", input={"query": "q"}),
            ToolResultEvent(name="web_search", content="found something"),
            TokenEvent(token=Token(text="The answer is 42.", is_final=False)),
            TokenEvent(token=Token(text="", is_final=True)),
        ]
        orch = _orch_with_events(vault, events)
        thread_id = orch.new_thread_id()
        async for _ in orch.send_turn_stream(thread_id, "what is the answer?"):
            pass

        log_events = orch.load_thread(thread_id)
        assistant_turns = [e for e in log_events if e["event_type"] == "turn_assistant"]
        assert len(assistant_turns) == 1
        assert assistant_turns[0]["payload"]["content"] == "The answer is 42."


# ---------------------------------------------------------------------------
# Project hub methods (Phase 3A)
# ---------------------------------------------------------------------------


def _make_candidate(slug: str = "my-proj", name: str = "My Proj") -> ProjectCandidate:
    return ProjectCandidate(
        name=name,
        slug=slug,
        path=f"/tmp/docs/{slug}",
        last_modified="2026-06-01T00:00:00+00:00",
        languages=["python"],
        has_readme=True,
        readme_path=f"/tmp/docs/{slug}/README.md",
        file_tree=["README.md"],
        git=None,
        markers=["pyproject.toml"],
        inferred_status="active",
    )


def _make_note(slug: str = "my-proj", status: str = "active") -> ProjectNote:
    return ProjectNote(
        name="My Proj",
        slug=slug,
        path=f"/tmp/docs/{slug}",
        stack=["python"],
        status=status,
        category="other",
        last_indexed=datetime.now(tz=UTC),
        discovered_at=datetime.now(tz=UTC),
        summary="A nice project.",
    )


class TestProjectDiscover:
    def test_returns_unindexed_candidates(self, orch: Orchestrator) -> None:
        candidates = [_make_candidate("alpha"), _make_candidate("beta")]
        indexed = [_make_note("alpha")]
        with (
            patch("orchestrator.orchestrator.discover", return_value=candidates),
            patch("orchestrator.orchestrator.read_project_notes", return_value=indexed),
        ):
            result = orch.discover_projects()

        slugs = [r["slug"] for r in result]
        assert "alpha" not in slugs
        assert "beta" in slugs

    def test_empty_when_all_indexed(self, orch: Orchestrator) -> None:
        candidates = [_make_candidate("alpha")]
        indexed = [_make_note("alpha")]
        with (
            patch("orchestrator.orchestrator.discover", return_value=candidates),
            patch("orchestrator.orchestrator.read_project_notes", return_value=indexed),
        ):
            result = orch.discover_projects()
        assert result == []

    def test_result_contains_shallow_fields_only(self, orch: Orchestrator) -> None:
        candidates = [_make_candidate("nexus")]
        with (
            patch("orchestrator.orchestrator.discover", return_value=candidates),
            patch("orchestrator.orchestrator.read_project_notes", return_value=[]),
        ):
            result = orch.discover_projects()
        assert len(result) == 1
        r = result[0]
        assert r["slug"] == "nexus"
        assert r["languages"] == ["python"]
        assert "summary" not in r  # no summarisation at discover time


class TestProjectIndex:
    async def test_indexes_given_slugs(self, orch: Orchestrator) -> None:
        candidate = _make_candidate("sample-app")
        note = _make_note("sample-app")
        with (
            patch("orchestrator.orchestrator.discover", return_value=[candidate]),
            patch(
                "orchestrator.orchestrator.summarize",
                new=AsyncMock(return_value=note),
            ),
            patch("orchestrator.orchestrator.write_project_note"),
        ):
            results = await orch.index_projects(["sample-app"])

        assert len(results) == 1
        assert results[0]["ok"] is True
        assert results[0]["slug"] == "sample-app"
        assert "note" in results[0]

    async def test_missing_slug_returns_error(self, orch: Orchestrator) -> None:
        with (
            patch("orchestrator.orchestrator.discover", return_value=[]),
            patch("orchestrator.orchestrator.summarize", new=AsyncMock()),
            patch("orchestrator.orchestrator.write_project_note"),
        ):
            results = await orch.index_projects(["no-such-project"])

        assert results[0]["ok"] is False
        assert "not found" in results[0]["error"]

    async def test_summarizer_exception_captured_per_slug(
        self, orch: Orchestrator
    ) -> None:
        candidate = _make_candidate("bad-proj")
        with (
            patch("orchestrator.orchestrator.discover", return_value=[candidate]),
            patch(
                "orchestrator.orchestrator.summarize",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch("orchestrator.orchestrator.write_project_note"),
        ):
            results = await orch.index_projects(["bad-proj"])

        assert results[0]["ok"] is False
        assert "boom" in results[0]["error"]

    async def test_returns_per_slug_results_for_mixed_batch(
        self, orch: Orchestrator
    ) -> None:
        candidate = _make_candidate("good")
        note = _make_note("good")
        with (
            patch("orchestrator.orchestrator.discover", return_value=[candidate]),
            patch("orchestrator.orchestrator.summarize", new=AsyncMock(return_value=note)),
            patch("orchestrator.orchestrator.write_project_note"),
        ):
            results = await orch.index_projects(["good", "missing"])

        ok_r = next(r for r in results if r["slug"] == "good")
        err_r = next(r for r in results if r["slug"] == "missing")
        assert ok_r["ok"] is True
        assert err_r["ok"] is False


class TestProjectAddManual:
    async def test_success_summarizes_and_stores(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        proj = tmp_path / "my-manual-proj"
        proj.mkdir()
        note = _make_note("my-manual-proj")
        with (
            patch("orchestrator.orchestrator.read_project_note", return_value=None),
            patch(
                "orchestrator.orchestrator.summarize",
                new=AsyncMock(return_value=note),
            ) as mock_summarize,
            patch("orchestrator.orchestrator.write_project_note") as mock_write,
        ):
            result = await orch.add_project_manual(str(proj))

        mock_summarize.assert_called_once()
        mock_write.assert_called_once()
        assert result["slug"] == "my-manual-proj"

    async def test_bypasses_marker_filtering(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        """A folder with zero detection markers must still be summarized."""
        proj = tmp_path / "bare-folder"
        proj.mkdir()  # completely empty — no .git, no README, no source files
        note = _make_note("bare-folder")
        with (
            patch("orchestrator.orchestrator.read_project_note", return_value=None),
            patch(
                "orchestrator.orchestrator.summarize",
                new=AsyncMock(return_value=note),
            ) as mock_summarize,
            patch("orchestrator.orchestrator.write_project_note"),
        ):
            await orch.add_project_manual(str(proj))

        mock_summarize.assert_called_once()
        candidate_arg = mock_summarize.call_args[0][0]
        assert candidate_arg.markers == []  # confirms no marker gate was applied

    async def test_nonexistent_path_raises_value_error(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            await orch.add_project_manual(str(tmp_path / "does-not-exist"))

    async def test_file_path_raises_value_error(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        f = tmp_path / "just-a-file.txt"
        f.write_text("hi", encoding="utf-8")
        with pytest.raises(ValueError, match="not a directory"):
            await orch.add_project_manual(str(f))

    async def test_slug_collision_different_path_raises_value_error(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        proj = tmp_path / "conflict"
        proj.mkdir()
        existing = _make_note("conflict")
        existing.path = "/somewhere/else/entirely"
        with patch("orchestrator.orchestrator.read_project_note", return_value=existing):
            with pytest.raises(ValueError, match="already indexed"):
                await orch.add_project_manual(str(proj))

    async def test_same_path_reindex_allowed(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        """Re-adding the exact same path (already indexed) is a re-index, not a conflict."""
        proj = tmp_path / "conflict"
        proj.mkdir()
        existing = _make_note("conflict")
        existing.path = str(proj.resolve())
        note = _make_note("conflict")
        with (
            patch("orchestrator.orchestrator.read_project_note", return_value=existing),
            patch(
                "orchestrator.orchestrator.summarize",
                new=AsyncMock(return_value=note),
            ) as mock_summarize,
            patch("orchestrator.orchestrator.write_project_note"),
        ):
            await orch.add_project_manual(str(proj))

        mock_summarize.assert_called_once()


class TestProjectRemove:
    def test_remove_existing_returns_true(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.delete_project_note", return_value=True):
            assert orch.remove_project("my-proj") is True

    def test_remove_missing_returns_false(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.delete_project_note", return_value=False):
            assert orch.remove_project("no-such") is False


class TestProjectOpen:
    def test_open_vscode(self, orch: Orchestrator, tmp_path: Path) -> None:
        orch._projects_config.scan_root = tmp_path
        note = _make_note("proj")
        note.path = str(tmp_path / "proj")
        with (
            patch("orchestrator.orchestrator.read_project_note", return_value=note),
            patch("orchestrator.orchestrator.subprocess") as mock_sp,
        ):
            orch.open_project("proj", "vscode")

        mock_sp.Popen.assert_called_once_with(["code", str(tmp_path / "proj")])

    def test_open_finder(self, orch: Orchestrator, tmp_path: Path) -> None:
        orch._projects_config.scan_root = tmp_path
        note = _make_note("proj")
        note.path = str(tmp_path / "proj")
        with (
            patch("orchestrator.orchestrator.read_project_note", return_value=note),
            patch("orchestrator.orchestrator.subprocess") as mock_sp,
        ):
            orch.open_project("proj", "finder")

        mock_sp.Popen.assert_called_once_with(["open", str(tmp_path / "proj")])

    def test_unknown_slug_raises_key_error(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.read_project_note", return_value=None):
            with pytest.raises(KeyError):
                orch.open_project("ghost", "vscode")

    def test_bad_target_raises_value_error(self, orch: Orchestrator, tmp_path: Path) -> None:
        orch._projects_config.scan_root = tmp_path
        note = _make_note("proj")
        note.path = str(tmp_path / "proj")
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            with pytest.raises(ValueError, match="Unknown target"):
                orch.open_project("proj", "emacs")

    def test_path_outside_scan_root_raises_permission_error(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        orch._projects_config.scan_root = tmp_path / "safe"
        note = _make_note("escape")
        note.path = "/etc/passwd"
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            with pytest.raises(PermissionError):
                orch.open_project("escape", "vscode")


class TestProjectList:
    def test_active_projects_sorted_first(self, orch: Orchestrator) -> None:
        t_old = datetime(2026, 1, 1, tzinfo=UTC)
        t_new = datetime(2026, 6, 1, tzinfo=UTC)
        notes = [
            _make_note("archived-old", status="archived"),
            _make_note("stale-mid", status="stale"),
            _make_note("active-new", status="active"),
        ]
        notes[0].last_indexed = t_old
        notes[1].last_indexed = t_old
        notes[2].last_indexed = t_new
        with patch("orchestrator.orchestrator.read_project_notes", return_value=notes):
            result = orch.list_projects()

        statuses = [r["status"] for r in result]
        assert statuses[0] == "active"
        assert statuses[-1] == "archived"

    def test_returns_serialisable_dicts(self, orch: Orchestrator) -> None:
        notes = [_make_note("alpha")]
        with patch("orchestrator.orchestrator.read_project_notes", return_value=notes):
            result = orch.list_projects()
        assert isinstance(result, list)
        assert isinstance(result[0], dict)
        assert result[0]["slug"] == "alpha"


# ---------------------------------------------------------------------------
# Phase 3W: project digest injection, meta events, project detail
# ---------------------------------------------------------------------------


class _CapturingCascade(_MockCascade):
    """Records the NexusRequest passed to complete_stream."""

    def __init__(self, events_to_yield: list | None = None) -> None:
        super().__init__(events_to_yield)
        self.last_request: NexusRequest | None = None

    async def complete_stream(
        self,
        request: NexusRequest,
        thread_yaml: dict,  # type: ignore[type-arg]
        live_history_filename: str,
    ) -> AsyncGenerator[Any, None]:
        self.last_request = request
        for event in self._events:
            yield event


def _capturing_orch(vault: Path) -> tuple[Orchestrator, _CapturingCascade]:
    cascade = _CapturingCascade()
    with patch("orchestrator.orchestrator.CascadeEngine", return_value=cascade):
        return Orchestrator(vault), cascade


class TestProjectDigestInjection:
    async def test_include_projects_injects_digest(self, vault: Path) -> None:
        orch, cascade = _capturing_orch(vault)
        notes = [_make_note("alpha"), _make_note("beta")]
        with patch("orchestrator.orchestrator.read_project_notes", return_value=notes):
            async for _ in orch.send_turn_stream(
                orch.new_thread_id(), "which projects use python?",
                include_projects=True,
            ):
                pass
        assert cascade.last_request is not None
        system_text = "\n".join(
            m.content for m in cascade.last_request.messages if m.role == "system"
        )
        assert "My Proj" in system_text
        assert "A nice project." in system_text

    async def test_flag_off_means_no_digest(self, vault: Path) -> None:
        orch, cascade = _capturing_orch(vault)
        notes = [_make_note("alpha")]
        with patch("orchestrator.orchestrator.read_project_notes", return_value=notes):
            async for _ in orch.send_turn_stream(
                orch.new_thread_id(), "which projects use python?",
            ):
                pass
        assert cascade.last_request is not None
        system_text = "\n".join(
            m.content for m in cascade.last_request.messages if m.role == "system"
        )
        assert "A nice project." not in system_text

    async def test_mode_reaches_request(self, vault: Path) -> None:
        orch, cascade = _capturing_orch(vault)
        async for _ in orch.send_turn_stream(
            orch.new_thread_id(), "hi", mode="research",
        ):
            pass
        assert cascade.last_request is not None
        assert cascade.last_request.mode == "research"


class TestMetaEventHandling:
    async def test_meta_event_streams_and_sets_model_id(self, vault: Path) -> None:
        from orchestrator.cascade.events import MetaEvent

        events = [
            MetaEvent(mode="light", requested_mode="auto", model="reliable/fast:free"),
            TokenEvent(token=Token(text="hey", is_final=False)),
            TokenEvent(token=Token(text="", is_final=True)),
        ]
        orch = _orch_with_events(vault, events)
        thread_id = orch.new_thread_id()
        with patch("orchestrator.orchestrator.is_worth_saving", return_value=True):
            streamed = [e async for e in orch.send_turn_stream(thread_id, "hi")]

        metas = [e for e in streamed if e["type"] == "meta"]
        assert metas == [{
            "type": "meta",
            "mode": "light",
            "requested_mode": "auto",
            "model": "reliable/fast:free",
        }]

        # JSONL turn_assistant.model_id must reflect the meta event's model
        from orchestrator.vault.event_log import EventLog
        log = EventLog(orch._log_path(thread_id))
        assistant = [e for e in log.read_all() if e.event_type == "turn_assistant"]
        assert assistant[0].payload["model_id"] == "reliable/fast:free"


class TestProjectDetail:
    def test_detail_reads_folder_fresh(self, orch: Orchestrator, tmp_path: Path) -> None:
        proj = tmp_path / "docs" / "fresh-proj"
        proj.mkdir(parents=True)
        (proj / "README.md").write_text("# Fresh\n\nHello.", encoding="utf-8")
        (proj / "main.py").write_text("print('hi')", encoding="utf-8")

        note = _make_note("fresh-proj")
        note.path = str(proj)
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            detail = orch.project_detail("fresh-proj")
            assert detail["readme"].startswith("# Fresh")
            assert "main.py" in detail["file_tree"]
            assert detail["git"] is None
            assert detail["folder_exists"] is True

            # Freshness: add a file AFTER indexing; detail must see it
            (proj / "new_file.py").write_text("", encoding="utf-8")
            detail2 = orch.project_detail("fresh-proj")
            assert "new_file.py" in detail2["file_tree"]

    def test_detail_unknown_slug_raises_keyerror(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.read_project_note", return_value=None):
            with pytest.raises(KeyError):
                orch.project_detail("nope")

    def test_detail_missing_folder_degrades(self, orch: Orchestrator) -> None:
        note = _make_note("gone-proj")
        note.path = "/tmp/docs/definitely-not-here-xyz"
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            detail = orch.project_detail("gone-proj")
        assert detail["folder_exists"] is False
        assert detail["readme"] is None
        assert detail["file_tree"] == []


# ---------------------------------------------------------------------------
# Phase 3B: run_command persistence + start/stop/status lifecycle
# ---------------------------------------------------------------------------


class TestUpdateProjectRunCommand:
    def test_persists_via_store(self, orch: Orchestrator) -> None:
        updated_note = _make_note("proj")
        updated_note.run_command = "python app.py"
        with patch(
            "orchestrator.orchestrator.update_run_command", return_value=updated_note
        ) as mock_update:
            result = orch.update_project_run_command("proj", "python app.py")

        mock_update.assert_called_once_with(orch.vault_root, "proj", "python app.py")
        assert result["run_command"] == "python app.py"

    def test_unknown_slug_raises_key_error(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.update_run_command", return_value=None):
            with pytest.raises(KeyError):
                orch.update_project_run_command("ghost", "python app.py")


class TestProjectStartStopStatus:
    async def test_start_delegates_to_runner_with_note_path(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        note = _make_note("proj")
        note.run_command = "python app.py"
        note.path = str(tmp_path / "proj")
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            orch._runner.start = AsyncMock(  # type: ignore[method-assign]
                return_value={"running": True, "pid": 111, "output": []}
            )
            result = await orch.start_project("proj")

        orch._runner.start.assert_called_once_with(
            "proj", "python app.py", Path(str(tmp_path / "proj"))
        )
        assert result["pid"] == 111

    async def test_start_unknown_slug_raises_key_error(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.read_project_note", return_value=None):
            with pytest.raises(KeyError):
                await orch.start_project("ghost")

    async def test_start_without_run_command_raises_value_error(
        self, orch: Orchestrator, tmp_path: Path
    ) -> None:
        note = _make_note("proj")
        note.run_command = None
        note.path = str(tmp_path / "proj")
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            with pytest.raises(ValueError, match="no run_command"):
                await orch.start_project("proj")

    async def test_stop_delegates_to_runner(self, orch: Orchestrator) -> None:
        note = _make_note("proj")
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            orch._runner.stop = AsyncMock(  # type: ignore[method-assign]
                return_value={"running": False, "pid": None, "output": ["bye"]}
            )
            result = await orch.stop_project("proj")

        orch._runner.stop.assert_called_once_with("proj")
        assert result["running"] is False

    async def test_stop_unknown_slug_raises_key_error(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.read_project_note", return_value=None):
            with pytest.raises(KeyError):
                await orch.stop_project("ghost")

    def test_status_delegates_to_runner(self, orch: Orchestrator) -> None:
        note = _make_note("proj")
        with patch("orchestrator.orchestrator.read_project_note", return_value=note):
            orch._runner.status = MagicMock(  # type: ignore[method-assign]
                return_value={"running": True, "pid": 222, "output": ["a", "b"]}
            )
            result = orch.project_status("proj")

        orch._runner.status.assert_called_once_with("proj")
        assert result["output"] == ["a", "b"]

    def test_status_unknown_slug_raises_key_error(self, orch: Orchestrator) -> None:
        with patch("orchestrator.orchestrator.read_project_note", return_value=None):
            with pytest.raises(KeyError):
                orch.project_status("ghost")


# ---------------------------------------------------------------------------
# Phase 3B: save-worthiness filter — persistence-only, streaming unaffected
# ---------------------------------------------------------------------------


def _stream_events(*texts: str) -> list:  # type: ignore[type-arg]
    """A token stream ending in a final empty token, like a real turn."""
    return [TokenEvent(token=Token(text=t, is_final=False)) for t in texts] + [
        TokenEvent(token=Token(text="", is_final=True))
    ]


class TestSaveWorthinessIntegration:
    async def test_trivial_turn_streams_but_is_not_persisted(self, vault: Path) -> None:
        o = _orch_with_events(vault, _stream_events("Hi there!"))
        thread_id = o.new_thread_id()
        streamed = [e async for e in o.send_turn_stream(thread_id, "hi")]

        # The user still sees every token — filtering is persistence-only.
        text = "".join(e["text"] for e in streamed if e.get("type") == "token")
        assert text == "Hi there!"

        # But nothing was written to the JSONL.
        assert o.load_thread(thread_id) == []

    async def test_substantive_turn_is_persisted(self, vault: Path) -> None:
        reply = (
            "A Markov chain is a sequence of states where the next state "
            "depends only on the current one."
        )
        o = _orch_with_events(vault, _stream_events(reply))
        thread_id = o.new_thread_id()
        streamed = [e async for e in o.send_turn_stream(
            thread_id, "hi, do you know what markov chains are"
        )]

        text = "".join(e["text"] for e in streamed if e.get("type") == "token")
        assert text == reply

        events = o.load_thread(thread_id)
        assert [e["event_type"] for e in events] == ["turn_user", "turn_assistant"]
        assert events[0]["payload"]["content"] == "hi, do you know what markov chains are"
        assert events[1]["payload"]["content"] == reply

    async def test_short_ack_with_short_reply_not_persisted(self, vault: Path) -> None:
        o = _orch_with_events(vault, _stream_events("ok"))
        thread_id = o.new_thread_id()
        async for _ in o.send_turn_stream(thread_id, "ok"):
            pass
        assert o.load_thread(thread_id) == []

    async def test_ack_with_substance_is_persisted(self, vault: Path) -> None:
        reply = "Good call — Flask's simplicity fits a small service like this well."
        o = _orch_with_events(vault, _stream_events(reply))
        thread_id = o.new_thread_id()
        async for _ in o.send_turn_stream(thread_id, "ok I'll use flask then"):
            pass
        events = o.load_thread(thread_id)
        assert [e["event_type"] for e in events] == ["turn_user", "turn_assistant"]

    async def test_filtered_first_turn_creates_no_jsonl_file(self, vault: Path) -> None:
        """A brand-new thread whose only turn is filtered never gets a JSONL
        file at all — list_threads() must not surface it."""
        o = _orch_with_events(vault, _stream_events("ok"))
        thread_id = o.new_thread_id()
        async for _ in o.send_turn_stream(thread_id, "hi"):
            pass

        id_short = thread_id.replace("-", "")[:16]
        jsonl = vault / "Orchestrator" / "Logs" / f"{id_short}.jsonl"
        assert not jsonl.exists()
        assert o.list_threads() == []

    async def test_second_turn_history_unaffected_by_first_turn_being_filtered(
        self, vault: Path
    ) -> None:
        """History sent to the model for turn 2 must not include a filtered
        turn 1 — there's nothing to include, since it was never saved.

        Two separate Orchestrator instances share the same vault/thread_id —
        _MockCascade yields a fixed reply per instance, so this is how a
        single mock harness gives turn 1 and turn 2 different replies.
        """
        o1 = _orch_with_events(vault, _stream_events("ok"))
        thread_id = o1.new_thread_id()

        # Turn 1: trivial ("hi" + short "ok" reply), filtered out.
        async for _ in o1.send_turn_stream(thread_id, "hi"):
            pass
        assert o1.load_thread(thread_id) == []

        # Turn 2: substantive, persisted — and it's turn_number 1, since the
        # filtered turn 1 never actually landed in history.
        reply2 = "Because the aligner needs at least three reference points to converge."
        o2 = _orch_with_events(vault, _stream_events(reply2))
        async for _ in o2.send_turn_stream(thread_id, "why does the aligner need 3 points?"):
            pass
        events = o2.load_thread(thread_id)
        assert len(events) == 2
        assert events[0]["payload"]["turn_number"] == 1

    async def test_live_projection_title_uses_pending_message_on_filtered_first_turn(
        self, vault: Path
    ) -> None:
        """Even though turn 1 gets filtered (so no markdown projection is
        written for it), if a LATER turn in the same thread does get saved,
        its projection title must still be derived correctly — proving
        _derive_live_filename's pending-message fallback didn't corrupt
        anything for the thread's eventual real first save."""
        o = _orch_with_events(vault, _stream_events("ok"))
        thread_id = o.new_thread_id()
        async for _ in o.send_turn_stream(thread_id, "hi"):
            pass  # filtered — no projection file yet
        assert list((vault / "Orchestrator" / "Live").glob("*.md")) == []

        reply = "Sure, here's a walkthrough of how the discovery scan works end to end."
        o2 = _orch_with_events(vault, _stream_events(reply))
        # Reuse the same thread_id so history/log path line up.
        async for _ in o2.send_turn_stream(thread_id, "walk me through the discovery scan"):
            pass
        live_files = list((vault / "Orchestrator" / "Live").glob("*.md"))
        assert len(live_files) == 1
        assert "walk-me-through" in live_files[0].name
