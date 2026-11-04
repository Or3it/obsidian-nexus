"""Tests for orchestrator/vault/projection.py."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.vault.event_log import Event
from orchestrator.vault.frontmatter import parse
from orchestrator.vault.projection import (
    kebab_title,
    project_thread,
    thread_filename,
)

THREAD_ID = "01935af4-0000-7000-8000-000000000001"


def _event(
    event_type: str,
    payload: dict,  # type: ignore[type-arg]
    *,
    timestamp: str = "2026-05-15T10:00:00.000+00:00",
    event_id: str = "00000000-0000-7000-8000-000000000001",
) -> Event:
    return Event(
        event_id=event_id,
        timestamp=timestamp,
        event_type=event_type,  # type: ignore[arg-type]
        schema_version=2,
        payload=payload,
    )


def _user(content: str = "hello", turn_number: int = 1, **kw: object) -> Event:
    return _event("turn_user", {"content": content, "turn_number": turn_number}, **kw)  # type: ignore[arg-type]


def _assistant(
    content: str = "hi",
    turn_number: int = 1,
    model_id: str = "claude-sonnet-4-6",
    **kw: object,
) -> Event:
    return _event(
        "turn_assistant",
        {
            "content": content,
            "turn_number": turn_number,
            "model_id": model_id,
            "auth_mode": "subscription",
            "estimated_tokens": 10,
            "duration_ms": 200,
        },
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# kebab_title
# ---------------------------------------------------------------------------


def test_kebab_basic() -> None:
    assert kebab_title("Designing the failover cascade!") == "designing-the-failover-cascade"


def test_kebab_empty_returns_empty() -> None:
    assert kebab_title("") == ""


def test_kebab_punctuation_only_returns_empty() -> None:
    assert kebab_title("!!!---") == ""


def test_kebab_truncates_without_breaking_word() -> None:
    long = "hello " * 20  # 120 chars → many words
    result = kebab_title(long)
    assert len(result) <= 64
    assert not result.endswith("-")


def test_kebab_long_single_word_truncates_to_max_len() -> None:
    result = kebab_title("a" * 200)
    assert len(result) <= 64


# ---------------------------------------------------------------------------
# thread_filename
# ---------------------------------------------------------------------------


def test_thread_filename_format() -> None:
    date = datetime(2026, 5, 15, tzinfo=UTC)
    name = thread_filename(date, THREAD_ID, "hello-world")
    assert name == "2026-05-15_thread-01935af4_hello-world.md"


def test_thread_filename_empty_title_falls_back_to_untitled() -> None:
    date = datetime(2026, 5, 15, tzinfo=UTC)
    name = thread_filename(date, THREAD_ID, "")
    assert name.startswith("2026-05-15_thread-01935af4_untitled-")


# ---------------------------------------------------------------------------
# project_thread — error cases
# ---------------------------------------------------------------------------


def test_empty_events_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        project_thread([], THREAD_ID, tmp_path)


# ---------------------------------------------------------------------------
# project_thread — happy path
# ---------------------------------------------------------------------------


def test_single_turn_produces_valid_markdown(tmp_path: Path) -> None:
    events = [_user("Say hello"), _assistant("Hello!")]
    path = project_thread(events, THREAD_ID, tmp_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    meta, body = parse(text)
    assert meta.thread_id == THREAD_ID
    assert meta.turn_count == 2
    assert "## Turn 1 — User" in body
    assert "## Turn 1 — Assistant (claude-sonnet-4-6)" in body


def test_filename_matches_pattern(tmp_path: Path) -> None:
    events = [_user("Hello world")]
    path = project_thread(events, THREAD_ID, tmp_path)
    assert path.name == "2026-05-15_thread-01935af4_hello-world.md"


# ---------------------------------------------------------------------------
# Bytewise fixture comparison
# ---------------------------------------------------------------------------


def test_projection_matches_fixture_bytewise(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """project_thread on sample_events.jsonl must match sample_projection.md exactly."""
    events: list[Event] = []
    with open(fixtures_dir / "sample_events.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                events.append(
                    Event(
                        event_id=obj["event_id"],
                        timestamp=obj["timestamp"],
                        event_type=obj["event_type"],
                        schema_version=obj.get("schema_version", 2),
                        payload=obj["payload"],
                    )
                )

    out_path = project_thread(events, THREAD_ID, tmp_path)
    actual = out_path.read_bytes()
    expected = (fixtures_dir / "sample_projection.md").read_bytes()
    assert actual == expected


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_idempotent_double_projection(tmp_path: Path) -> None:
    events = [_user("Idempotence check"), _assistant("Yes.")]
    path1 = project_thread(events, THREAD_ID, tmp_path)
    bytes1 = path1.read_bytes()
    path2 = project_thread(events, THREAD_ID, tmp_path)
    bytes2 = path2.read_bytes()
    assert bytes1 == bytes2


# ---------------------------------------------------------------------------
# Re-parse round-trip
# ---------------------------------------------------------------------------


def test_projected_file_re_parses_correctly(tmp_path: Path) -> None:
    events = [_user("Round-trip"), _assistant("Confirmed.")]
    path = project_thread(events, THREAD_ID, tmp_path)
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert meta.thread_id == THREAD_ID
    assert meta.active_model == "claude-sonnet-4-6"
    assert meta.models_used[0].turns == [1]


# ---------------------------------------------------------------------------
# Model coalescing
# ---------------------------------------------------------------------------


def test_same_model_consecutive_turns_coalesced(tmp_path: Path) -> None:
    events = [
        _user(
            "q1",
            turn_number=1,
            timestamp="2026-05-15T10:00:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000001",
        ),
        _assistant(
            "a1",
            turn_number=1,
            timestamp="2026-05-15T10:00:01.000+00:00",
            event_id="00000000-0000-7000-8000-000000000002",
        ),
        _user(
            "q2",
            turn_number=2,
            timestamp="2026-05-15T10:00:02.000+00:00",
            event_id="00000000-0000-7000-8000-000000000003",
        ),
        _assistant(
            "a2",
            turn_number=2,
            timestamp="2026-05-15T10:00:03.000+00:00",
            event_id="00000000-0000-7000-8000-000000000004",
        ),
    ]
    path = project_thread(events, THREAD_ID, tmp_path)
    meta, _ = parse(path.read_text(encoding="utf-8"))
    # Both assistant turns on the same model → one entry with two turn numbers
    assert len(meta.models_used) == 1
    assert meta.models_used[0].turns == [1, 2]


# ---------------------------------------------------------------------------
# thread_pin events → pinned_model in frontmatter
# ---------------------------------------------------------------------------


def _pin(
    model: object,
    *,
    timestamp: str = "2026-05-15T10:01:00.000+00:00",
    event_id: str = "00000000-0000-7000-8000-000000000099",
) -> Event:
    return _event("thread_pin", {"model": model}, timestamp=timestamp, event_id=event_id)


def test_thread_pin_sets_pinned_model(tmp_path: Path) -> None:
    events = [_user(), _assistant(), _pin("claude-opus-4-7")]
    path = project_thread(events, THREAD_ID, tmp_path)
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert meta.pinned_model == "claude-opus-4-7"


def test_thread_pin_null_clears_pinned_model(tmp_path: Path) -> None:
    events = [
        _user(
            timestamp="2026-05-15T10:00:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000001",
        ),
        _assistant(
            timestamp="2026-05-15T10:00:01.000+00:00",
            event_id="00000000-0000-7000-8000-000000000002",
        ),
        _pin(
            "claude-opus-4-7",
            timestamp="2026-05-15T10:01:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000003",
        ),
        _pin(
            None,
            timestamp="2026-05-15T10:02:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000004",
        ),
    ]
    path = project_thread(events, THREAD_ID, tmp_path)
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert meta.pinned_model is None


def test_latest_thread_pin_wins(tmp_path: Path) -> None:
    events = [
        _user(
            timestamp="2026-05-15T10:00:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000001",
        ),
        _assistant(
            timestamp="2026-05-15T10:00:01.000+00:00",
            event_id="00000000-0000-7000-8000-000000000002",
        ),
        _pin(
            "claude-opus-4-7",
            timestamp="2026-05-15T10:01:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000003",
        ),
        _pin(
            "minimax/minimax-m2.5",
            timestamp="2026-05-15T10:02:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000004",
        ),
    ]
    path = project_thread(events, THREAD_ID, tmp_path)
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert meta.pinned_model == "minimax/minimax-m2.5"


def test_no_thread_pin_leaves_pinned_model_none(tmp_path: Path) -> None:
    events = [_user(), _assistant()]
    path = project_thread(events, THREAD_ID, tmp_path)
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert meta.pinned_model is None


def test_model_switch_creates_new_entry(tmp_path: Path) -> None:
    sonnet_ev = _assistant(
        "a1",
        turn_number=1,
        model_id="claude-sonnet-4-6",
        timestamp="2026-05-15T10:00:01.000+00:00",
        event_id="00000000-0000-7000-8000-000000000002",
    )
    opus_ev = _event(
        "turn_assistant",
        {"content": "a2", "turn_number": 2, "model_id": "claude-opus-4-7",
         "auth_mode": "subscription", "estimated_tokens": 5, "duration_ms": 100},
        timestamp="2026-05-15T10:00:03.000+00:00",
        event_id="00000000-0000-7000-8000-000000000004",
    )
    events = [
        _user(
            "q1",
            turn_number=1,
            timestamp="2026-05-15T10:00:00.000+00:00",
            event_id="00000000-0000-7000-8000-000000000001",
        ),
        sonnet_ev,
        _user(
            "q2",
            turn_number=2,
            timestamp="2026-05-15T10:00:02.000+00:00",
            event_id="00000000-0000-7000-8000-000000000003",
        ),
        opus_ev,
    ]
    path = project_thread(events, THREAD_ID, tmp_path)
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert len(meta.models_used) == 2
    assert meta.models_used[0].id == "claude-sonnet-4-6"
    assert meta.models_used[1].id == "claude-opus-4-7"
    assert meta.active_model == "claude-opus-4-7"
