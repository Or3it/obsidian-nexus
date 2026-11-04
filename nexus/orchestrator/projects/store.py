"""Vault storage for project notes (Projects/<slug>.md).

Notes are the canonical record for an approved project.  The dashboard
and chat inject them as context; removing a note deletes only the note,
never the real project folder.
"""

from __future__ import annotations

import logging
from pathlib import Path

import frontmatter as fm  # type: ignore[import-untyped]
import yaml

from .types import ProjectNote

log = logging.getLogger(__name__)


def write_project_note(vault_root: Path, note: ProjectNote) -> Path:
    """Write *note* to ``{vault_root}/Projects/{slug}.md``.

    Idempotent: if the note already exists, overwrites it and refreshes
    ``last_indexed``.  Preserves ``discovered_at`` and ``run_command``
    from the existing note so user edits and re-index both survive.
    """
    projects_dir = vault_root / "Projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    note_path = projects_dir / f"{note.slug}.md"

    if note_path.exists():
        existing = _parse_note_file(note_path)
        if existing is not None:
            updates: dict[str, object] = {}
            if note.discovered_at is None and existing.discovered_at is not None:
                updates["discovered_at"] = existing.discovered_at
            if note.run_command is None and existing.run_command is not None:
                updates["run_command"] = existing.run_command
            if updates:
                note = note.model_copy(update=updates)

    note_path.write_text(_serialize(note), encoding="utf-8")
    return note_path


def read_project_notes(vault_root: Path) -> list[ProjectNote]:
    """Return all notes in ``{vault_root}/Projects/``, sorted by slug."""
    projects_dir = vault_root / "Projects"
    if not projects_dir.exists():
        return []
    notes: list[ProjectNote] = []
    for path in sorted(projects_dir.glob("*.md")):
        note = _parse_note_file(path)
        if note is not None:
            notes.append(note)
        else:
            log.warning("Skipping unreadable project note: %s", path)
    return notes


def read_project_note(vault_root: Path, slug: str) -> ProjectNote | None:
    """Return the note for *slug*, or None if it does not exist."""
    note_path = vault_root / "Projects" / f"{slug}.md"
    if not note_path.exists():
        return None
    return _parse_note_file(note_path)


def update_run_command(vault_root: Path, slug: str, run_command: str) -> ProjectNote | None:
    """Set ``run_command`` on an existing note and persist it.

    All other fields (summary, stack, discovered_at, last_indexed, ...) are
    preserved untouched. Returns the updated note, or None if the slug has no
    note yet.
    """
    note = read_project_note(vault_root, slug)
    if note is None:
        return None
    updated = note.model_copy(update={"run_command": run_command})
    note_path = vault_root / "Projects" / f"{slug}.md"
    note_path.write_text(_serialize(updated), encoding="utf-8")
    return updated


def delete_project_note(vault_root: Path, slug: str) -> bool:
    """Delete the note file for *slug*.  Never touches the real project folder.

    Returns True if the note existed and was removed; False if not found.
    """
    note_path = vault_root / "Projects" / f"{slug}.md"
    if not note_path.exists():
        return False
    note_path.unlink()
    return True


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialize(note: ProjectNote) -> str:
    """Render a ProjectNote to markdown with YAML frontmatter."""
    fm_dict = note.model_dump(mode="json", exclude={"summary"})
    fm_yaml = yaml.safe_dump(
        fm_dict,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    body = note.summary or ""
    return f"---\n{fm_yaml}---\n\n{body}\n"


def _parse_note_file(path: Path) -> ProjectNote | None:
    """Parse a Projects/*.md file back into a ProjectNote."""
    try:
        raw = path.read_text(encoding="utf-8")
        post = fm.loads(raw)
        data = dict(post.metadata)
        data["summary"] = post.content.strip()
        return ProjectNote(**data)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not parse project note %s: %s", path, exc)
        return None
