"""Tests for orchestrator.projects.store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from orchestrator.projects.store import (
    delete_project_note,
    read_project_note,
    read_project_notes,
    update_run_command,
    write_project_note,
)
from orchestrator.projects.types import ProjectNote

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _note(
    name: str = "My Project",
    slug: str = "my-project",
    stack: list[str] | None = None,
    status: str = "active",
    category: str = "other",
    summary: str = "A nice project.",
    run_command: str | None = None,
    discovered_at: datetime | None = None,
    last_indexed: datetime | None = None,
) -> ProjectNote:
    return ProjectNote(
        name=name,
        slug=slug,
        path=f"/tmp/{slug}",
        stack=stack or ["python"],
        status=status,
        category=category,
        run_command=run_command,
        preview=None,
        last_indexed=last_indexed or datetime.now(tz=UTC),
        discovered_at=discovered_at or datetime.now(tz=UTC),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# write + read round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    n = _note(
        name="Sample Project",
        slug="sample-project",
        stack=["python", "flask", "telegram"],
        status="active",
        category="trading",
        summary="Tracks trades automatically.",
    )
    write_project_note(tmp_path, n)
    back = read_project_note(tmp_path, "sample-project")
    assert back is not None
    assert back.name == "Sample Project"
    assert back.slug == "sample-project"
    assert back.stack == ["python", "flask", "telegram"]
    assert back.status == "active"
    assert back.category == "trading"
    assert back.summary == "Tracks trades automatically."
    assert back.path == "/tmp/sample-project"


def test_write_creates_projects_dir(tmp_path: Path) -> None:
    n = _note()
    write_project_note(tmp_path, n)
    assert (tmp_path / "Projects").is_dir()
    assert (tmp_path / "Projects" / "my-project.md").exists()


def test_write_returns_path(tmp_path: Path) -> None:
    n = _note(slug="nexus")
    p = write_project_note(tmp_path, n)
    assert p == tmp_path / "Projects" / "nexus.md"


def test_summary_is_note_body_not_frontmatter(tmp_path: Path) -> None:
    n = _note(summary="This summary is the body.")
    write_project_note(tmp_path, n)
    raw = (tmp_path / "Projects" / "my-project.md").read_text()
    # Summary must appear after the closing --- of frontmatter
    body_start = raw.find("---\n\n")
    assert body_start != -1
    assert "This summary is the body." in raw[body_start:]
    # Confirm it's NOT in the frontmatter section
    fm_section = raw[: raw.find("---\n\n")]
    assert "This summary is the body." not in fm_section


def test_run_command_round_trips(tmp_path: Path) -> None:
    n = _note(run_command="python main.py")
    write_project_note(tmp_path, n)
    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.run_command == "python main.py"


def test_null_fields_round_trip(tmp_path: Path) -> None:
    n = _note()
    write_project_note(tmp_path, n)
    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.run_command is None
    assert back.preview is None


# ---------------------------------------------------------------------------
# read_project_notes — list all
# ---------------------------------------------------------------------------


def test_read_project_notes_returns_all(tmp_path: Path) -> None:
    for slug in ("alpha", "beta", "gamma"):
        write_project_note(tmp_path, _note(slug=slug))
    notes = read_project_notes(tmp_path)
    slugs = {n.slug for n in notes}
    assert slugs == {"alpha", "beta", "gamma"}


def test_read_project_notes_empty_dir(tmp_path: Path) -> None:
    assert read_project_notes(tmp_path) == []


def test_read_project_notes_missing_dir(tmp_path: Path) -> None:
    assert read_project_notes(tmp_path / "nonexistent") == []


def test_read_project_notes_sorted_by_slug(tmp_path: Path) -> None:
    for slug in ("zzz", "aaa", "mmm"):
        write_project_note(tmp_path, _note(slug=slug))
    notes = read_project_notes(tmp_path)
    assert [n.slug for n in notes] == ["aaa", "mmm", "zzz"]


# ---------------------------------------------------------------------------
# Idempotency / re-index behaviour
# ---------------------------------------------------------------------------


def test_reindex_overwrites_summary_and_stack(tmp_path: Path) -> None:
    original = _note(summary="Old summary.", stack=["python"])
    write_project_note(tmp_path, original)

    updated = _note(
        summary="New summary with more detail.",
        stack=["python", "redis"],
        last_indexed=datetime.now(tz=UTC),
    )
    write_project_note(tmp_path, updated)

    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.summary == "New summary with more detail."
    assert back.stack == ["python", "redis"]


def test_reindex_preserves_discovered_at(tmp_path: Path) -> None:
    original_dt = datetime(2026, 1, 1, tzinfo=UTC)
    original = _note(discovered_at=original_dt)
    write_project_note(tmp_path, original)

    # Re-index: explicitly pass discovered_at=None so preservation logic fires
    updated = ProjectNote(
        name="My Project",
        slug="my-project",
        path="/tmp/my-project",
        stack=["python"],
        status="active",
        category="other",
        last_indexed=datetime.now(tz=UTC),
        discovered_at=None,
        summary="Updated summary.",
    )
    write_project_note(tmp_path, updated)

    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.discovered_at is not None
    assert back.discovered_at.replace(tzinfo=UTC) == original_dt


def test_reindex_preserves_user_edited_run_command(tmp_path: Path) -> None:
    original = _note(run_command="make run")
    write_project_note(tmp_path, original)

    # Re-index with run_command=None — should keep the user's value
    updated = _note(run_command=None)
    write_project_note(tmp_path, updated)

    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.run_command == "make run"


def test_reindex_allows_updating_run_command(tmp_path: Path) -> None:
    original = _note(run_command="old command")
    write_project_note(tmp_path, original)

    updated = _note(run_command="new command")
    write_project_note(tmp_path, updated)

    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.run_command == "new command"


def test_reindex_refreshes_last_indexed(tmp_path: Path) -> None:
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 7, tzinfo=UTC)
    write_project_note(tmp_path, _note(last_indexed=t1))
    write_project_note(tmp_path, _note(last_indexed=t2))

    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.last_indexed is not None
    # last_indexed should reflect the second write
    assert back.last_indexed.year == 2026
    assert back.last_indexed.month == 6


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_note_file(tmp_path: Path) -> None:
    write_project_note(tmp_path, _note(slug="to-delete"))
    assert (tmp_path / "Projects" / "to-delete.md").exists()
    result = delete_project_note(tmp_path, "to-delete")
    assert result is True
    assert not (tmp_path / "Projects" / "to-delete.md").exists()


def test_delete_missing_slug_returns_false(tmp_path: Path) -> None:
    result = delete_project_note(tmp_path, "does-not-exist")
    assert result is False


def test_delete_does_not_touch_other_notes(tmp_path: Path) -> None:
    write_project_note(tmp_path, _note(slug="keep"))
    write_project_note(tmp_path, _note(slug="remove"))
    delete_project_note(tmp_path, "remove")
    assert (tmp_path / "Projects" / "keep.md").exists()
    assert read_project_note(tmp_path, "keep") is not None


def test_delete_never_touches_outside_projects(tmp_path: Path) -> None:
    """Ensure delete is scoped to Projects/ and cannot escape to the real folder."""
    real_project = tmp_path / "sample-app"
    real_project.mkdir()
    (real_project / "main.py").write_text("code", encoding="utf-8")

    # Attempt to delete a note with the same name as a real folder
    result = delete_project_note(tmp_path, "sample-app")
    assert result is False  # note doesn't exist
    assert real_project.exists()  # real folder untouched


# ---------------------------------------------------------------------------
# read_project_note — missing
# ---------------------------------------------------------------------------


def test_read_project_note_missing_returns_none(tmp_path: Path) -> None:
    assert read_project_note(tmp_path, "no-such-project") is None


# ---------------------------------------------------------------------------
# update_run_command
# ---------------------------------------------------------------------------


def test_update_run_command_persists_and_preserves_other_fields(tmp_path: Path) -> None:
    original = _note(run_command=None, summary="Original summary.")
    write_project_note(tmp_path, original)

    updated = update_run_command(tmp_path, "my-project", "python app.py")
    assert updated is not None
    assert updated.run_command == "python app.py"
    assert updated.summary == "Original summary."

    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.run_command == "python app.py"
    assert back.summary == "Original summary."


def test_update_run_command_overwrites_existing_value(tmp_path: Path) -> None:
    write_project_note(tmp_path, _note(run_command="old command"))
    update_run_command(tmp_path, "my-project", "new command")
    back = read_project_note(tmp_path, "my-project")
    assert back is not None
    assert back.run_command == "new command"


def test_update_run_command_unknown_slug_returns_none(tmp_path: Path) -> None:
    assert update_run_command(tmp_path, "no-such-project", "python app.py") is None
