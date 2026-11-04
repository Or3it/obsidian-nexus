"""Tests for orchestrator/vault/frontmatter.py."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from orchestrator.vault.frontmatter import (
    ConversationFrontmatter,
    ModelUsage,
    StyleProfileFM,
    parse,
    serialise,
)


def _minimal(
    *,
    thread_id: str = "test-thread-id",
    created: datetime | None = None,
    updated: datetime | None = None,
) -> ConversationFrontmatter:
    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    return ConversationFrontmatter(
        thread_id=thread_id,
        title="test-title",
        created=created or now,
        updated=updated or now,
        active_model="claude-sonnet-4-6",
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_identity() -> None:
    meta = _minimal()
    text = serialise(meta, "some body content")
    meta2, body = parse(text)
    assert meta == meta2
    assert body == "some body content"


def test_round_trip_with_models_used() -> None:
    meta = _minimal()
    meta.models_used = [
        ModelUsage(
            id="claude-sonnet-4-6",
            auth_mode="subscription",
            turns=[1, 2, 3],
            estimated_tokens=1500,
        )
    ]
    meta2, _ = parse(serialise(meta, "body"))
    assert meta2.models_used[0].id == "claude-sonnet-4-6"
    assert meta2.models_used[0].turns == [1, 2, 3]
    assert meta2.models_used[0].estimated_tokens == 1500


# ---------------------------------------------------------------------------
# Datetime timezone round-trip
# ---------------------------------------------------------------------------


def test_utc_datetime_round_trips() -> None:
    meta = _minimal(created=datetime(2026, 5, 15, 14, 32, 0, tzinfo=UTC))
    meta2, _ = parse(serialise(meta, ""))
    assert meta2.created.utcoffset() == timedelta(0)
    assert meta2.created.hour == 14


def test_non_utc_timezone_offset_preserved() -> None:
    tz = timezone(timedelta(hours=-5))
    meta = _minimal(created=datetime(2026, 5, 15, 9, 0, 0, tzinfo=tz))
    meta2, _ = parse(serialise(meta, ""))
    assert meta2.created.isoformat() == meta.created.isoformat()
    assert meta2.created.utcoffset() == timedelta(hours=-5)


# ---------------------------------------------------------------------------
# Enum rejection
# ---------------------------------------------------------------------------


def test_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        ConversationFrontmatter(
            thread_id="x",
            title="x",
            created=datetime(2026, 1, 1, tzinfo=UTC),
            updated=datetime(2026, 1, 1, tzinfo=UTC),
            active_model="m",
            status="deleted",  # type: ignore[arg-type]
        )


def test_rejects_invalid_style_format() -> None:
    with pytest.raises(ValidationError):
        StyleProfileFM(format="bullet_points")  # type: ignore[arg-type]


def test_rejects_invalid_model_usage_auth_mode() -> None:
    with pytest.raises(ValidationError):
        ModelUsage(
            id="model",
            auth_mode="enterprise",  # type: ignore[arg-type]
            turns=[1],
        )


# ---------------------------------------------------------------------------
# Optional field defaults
# ---------------------------------------------------------------------------


def test_optional_fields_default_on_minimal_parse() -> None:
    """A file with only the required Phase 1 fields parses without error."""
    minimal_yaml = (
        "---\n"
        "type: conversation\n"
        "schema_version: 2\n"
        "thread_id: abc-123\n"
        "title: test\n"
        "created: '2026-05-15T10:00:00Z'\n"
        "updated: '2026-05-15T10:00:00Z'\n"
        "active_model: claude-sonnet-4-6\n"
        "---\n\nbody\n"
    )
    meta, _ = parse(minimal_yaml)
    assert meta.parent_thread is None
    assert meta.pinned_model is None
    assert meta.tags == []
    assert meta.related_threads == []
    assert meta.handovers == []
    assert meta.status == "live"
    assert meta.turn_count == 0


# ---------------------------------------------------------------------------
# Empty list serialisation
# ---------------------------------------------------------------------------


def test_empty_models_used_serialises_not_omitted() -> None:
    meta = _minimal()
    text = serialise(meta, "")
    assert "models_used: []" in text


def test_empty_tags_serialises_not_omitted() -> None:
    meta = _minimal()
    text = serialise(meta, "")
    assert "tags: []" in text
