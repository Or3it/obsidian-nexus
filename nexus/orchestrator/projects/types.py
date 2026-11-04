"""Types for the project discovery and vault-note layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field


@dataclass
class ProjectCandidate:
    name: str
    slug: str
    path: str
    last_modified: str
    languages: list[str]
    has_readme: bool
    readme_path: str | None
    file_tree: list[str]
    git: dict[str, str | bool | None] | None
    markers: list[str]
    inferred_status: str

    def __post_init__(self) -> None:
        if not isinstance(self.languages, list):
            self.languages = list(self.languages)
        if not isinstance(self.file_tree, list):
            self.file_tree = list(self.file_tree)
        if not isinstance(self.markers, list):
            self.markers = list(self.markers)


class ProjectNote(BaseModel):
    """Frontmatter schema for Projects/<slug>.md vault notes.

    Defined here; written by T02.
    """

    name: str
    slug: str
    path: str
    stack: list[str] = Field(default_factory=list)
    status: str = "active"
    category: str = "other"
    run_command: str | None = None
    preview: str | None = None
    last_indexed: datetime | None = None
    discovered_at: datetime | None = None
    summary: str = ""
