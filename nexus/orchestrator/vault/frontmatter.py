"""YAML frontmatter validation and (de)serialisation for vault markdown files.

Uses python-frontmatter for parsing and yaml.safe_dump for serialisation so
that field order is stable and diffs are readable.
"""

from datetime import datetime
from typing import Literal

import frontmatter as fm  # type: ignore[import-untyped]
import yaml
from pydantic import BaseModel, Field


class ModelUsage(BaseModel):
    id: str
    auth_mode: Literal["subscription", "api_key_free_tier", "api_key_paid", "local"]
    turns: list[int]
    estimated_tokens: int = 0


class StyleProfileFM(BaseModel):
    format: Literal["prose", "structured", "mixed"] = "prose"
    verbosity: Literal["concise", "default", "detailed"] = "default"
    posture: Literal["assistant", "peer", "tutor"] = "assistant"


class ConversationFrontmatter(BaseModel):
    type: Literal["conversation"] = "conversation"
    schema_version: int = 2
    thread_id: str
    parent_thread: str | None = None
    title: str
    created: datetime
    updated: datetime
    status: Literal["live", "archived", "paused", "locked"] = "live"
    turn_count: int = 0
    pro_quota_consumed_estimate_pct: float = 0.0
    api_spend_usd: float = 0.0
    api_spend_enabled: bool = False
    budget_cap_usd: float = 2.00
    models_used: list[ModelUsage] = Field(default_factory=list)
    active_model: str
    pinned_model: str | None = None
    jurisdiction_locked: bool = False
    pro_quota_exhausted_until: datetime | None = None
    style_profile: StyleProfileFM = Field(default_factory=StyleProfileFM)
    tags: list[str] = Field(default_factory=list)
    related_threads: list[str] = Field(default_factory=list)
    related_notes: list[str] = Field(default_factory=list)
    handovers: list[str] = Field(default_factory=list)


def parse(md_text: str) -> tuple[ConversationFrontmatter, str]:
    """Parse a markdown file's frontmatter and body."""
    post = fm.loads(md_text)
    return ConversationFrontmatter(**post.metadata), post.content


def serialise(meta: ConversationFrontmatter, body: str) -> str:
    """Serialise frontmatter + body to a markdown string.

    Uses yaml.safe_dump with sort_keys=False to preserve declaration order
    and produce stable diffs. python-frontmatter's default writer sorts keys
    alphabetically which makes review harder.
    """
    fm_dict = meta.model_dump(mode="json", exclude_none=False)
    fm_yaml = yaml.safe_dump(
        fm_dict,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return f"---\n{fm_yaml}---\n\n{body}\n"
