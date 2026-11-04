# P1-T04 — Markdown Projection + Frontmatter

## Goal

Project the JSONL event log into a human-readable markdown conversation file with valid YAML frontmatter. Markdown is a derived artifact — always rebuildable from JSONL.

## Read first

- `shared/yaml-schemas.md` (conversation frontmatter — Phase 1 minimum subset)
- `shared/file-conventions.md` (filename pattern, kebab-titles, timestamps)
- `shared/data-architecture.md` ("JSONL is canonical, markdown is projected")

## Files to create or replace

```
orchestrator/vault/frontmatter.py
orchestrator/vault/projection.py
tests/test_frontmatter.py
tests/test_projection.py
tests/fixtures/sample_events.jsonl    ← input fixture
tests/fixtures/sample_projection.md   ← expected output fixture
```

## `orchestrator/vault/frontmatter.py`

Validates and (de)serialises the conversation frontmatter. Built around `python-frontmatter` (which delegates YAML to PyYAML) but with Pydantic validation layered on top.

```python
from pathlib import Path
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
import frontmatter as fm   # python-frontmatter
import yaml

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
    """Parse a markdown file into (frontmatter, body)."""
    post = fm.loads(md_text)
    return ConversationFrontmatter(**post.metadata), post.content

def serialise(meta: ConversationFrontmatter, body: str) -> str:
    """Serialise frontmatter + body to a markdown string."""
    # Use yaml.safe_dump with sort_keys=False for stable field order.
    fm_dict = meta.model_dump(mode="json", exclude_none=False)
    fm_yaml = yaml.safe_dump(fm_dict, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{fm_yaml}---\n\n{body}\n"
```

Note: prefer `serialise` over `fm.dumps` so we get deterministic YAML output. `python-frontmatter`'s default writer alphabetises keys and that breaks our stable diffs.

## `orchestrator/vault/projection.py`

```python
from pathlib import Path
from datetime import datetime
import re
from .event_log import EventLog, Event
from .frontmatter import ConversationFrontmatter, ModelUsage, serialise

KEBAB_NON_ALNUM = re.compile(r"[^a-z0-9]+")

def kebab_title(seed_text: str, max_len: int = 64) -> str:
    """Generate a kebab-case title from a seed string."""
    lowered = seed_text.lower()
    kebab = KEBAB_NON_ALNUM.sub("-", lowered).strip("-")
    if not kebab:
        return ""
    if len(kebab) <= max_len:
        return kebab
    # Truncate without breaking a word.
    cut = kebab[:max_len]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut

def thread_filename(date: datetime, thread_id: str, title: str) -> str:
    """Return `{YYYY-MM-DD}_thread-{id-short8}_{kebab-title}.md`."""
    id_short = thread_id.replace("-", "")[:8]
    safe_title = title if title else f"untitled-{id_short}"
    return f"{date.strftime('%Y-%m-%d')}_thread-{id_short}_{safe_title}.md"

def project_thread(events: list[Event], thread_id: str, vault_root: Path) -> Path:
    """
    Build the markdown projection of a thread from its event log.
    Writes to Orchestrator/Live/{filename} and returns the path written.
    Idempotent: re-running with the same events produces a bytewise-identical file.
    """
    if not events:
        raise ValueError("Cannot project an empty event log.")

    # Derive frontmatter from events.
    first_user_event = next((e for e in events if e.event_type == "turn_user"), None)
    title_seed = ""
    if first_user_event:
        title_seed = first_user_event.payload.get("content", "")[:200]
    title = kebab_title(title_seed) or f"untitled-{thread_id.replace('-', '')[:8]}"

    created = datetime.fromisoformat(events[0].timestamp)
    updated = datetime.fromisoformat(events[-1].timestamp)

    turn_count = sum(1 for e in events if e.event_type in ("turn_user", "turn_assistant"))

    # Aggregate models_used from turn_assistant events.
    models_used = _aggregate_models(events)
    active_model = models_used[-1].id if models_used else "claude-sonnet-4-6"

    meta = ConversationFrontmatter(
        thread_id=thread_id,
        title=title,
        created=created,
        updated=updated,
        turn_count=turn_count,
        models_used=models_used,
        active_model=active_model,
    )

    body = _render_body(events)
    md = serialise(meta, body)

    live_dir = vault_root / "Orchestrator" / "Live"
    live_dir.mkdir(parents=True, exist_ok=True)
    out_path = live_dir / thread_filename(created, thread_id, title)
    out_path.write_text(md, encoding="utf-8")
    return out_path

def _aggregate_models(events: list[Event]) -> list[ModelUsage]:
    """Walk turn_assistant events; coalesce consecutive same-model runs."""
    ...   # implementation

def _render_body(events: list[Event]) -> str:
    """Render the conversation body. Each turn formatted with role marker + content."""
    ...   # implementation
```

Body format (Phase 1):

```markdown
## Turn 1 — User
{content}

## Turn 1 — Assistant (claude-sonnet-4-6)
{content}

## Turn 2 — User
{content}

## Turn 2 — Assistant (claude-sonnet-4-6)
{content}
```

Adjacent same-model assistant turns share a single `models_used` entry with multiple turns in the `turns` list.

## Tests

`tests/test_frontmatter.py`:

- Round-trip: build a `ConversationFrontmatter`, serialise, parse, equal to original.
- Reject invalid `status`, `auth_mode`, `style_profile.format` enum values.
- Datetimes round-trip with timezone offset preserved.
- Missing optional fields default correctly on parse.
- Empty `models_used` list serialises as `models_used: []`, not omitted.

`tests/test_projection.py`:

- Empty event list raises `ValueError`.
- Single user + single assistant turn produces a markdown file with valid frontmatter and the body in the documented format.
- Filename matches `{YYYY-MM-DD}_thread-{id8}_{title}.md`.
- `kebab_title("Designing the failover cascade!")` → `"designing-the-failover-cascade"`.
- `kebab_title("")` returns `""` (caller substitutes `untitled-{id8}`).
- `kebab_title("a" * 200)` truncates to ≤64 chars without breaking at non-word boundary.
- Two-turn projection (one user, one assistant) matches `tests/fixtures/sample_projection.md` bytewise.
- Re-running projection with the same input twice produces a bytewise-identical file (idempotence).
- Reading a projected file with `frontmatter.parse` yields the same metadata back.

## Definition of done

- [ ] All four source files exist.
- [ ] `pytest tests/test_frontmatter.py tests/test_projection.py` passes.
- [ ] `ruff` and `mypy` clean.
- [ ] Sample projection file exists and matches the documented body format.

## Definition of NOT done

- Don't archive completed threads yet (Phase 2 concern).
- Don't write the JSONL log here; that's P1-T03's job. This module *consumes* the log.
- Don't introduce a database. Frontmatter is read directly from files.
- Don't implement Haiku-based title generation. Phase 1 uses kebab-of-first-message.
