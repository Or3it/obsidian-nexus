"""Projects JSONL event logs into markdown conversation files.

Markdown is a derived artifact — always rebuildable from the JSONL source.
If a projected file and its JSONL source disagree, JSONL wins.
"""

import re
from datetime import datetime
from pathlib import Path

from .event_log import Event
from .frontmatter import ConversationFrontmatter, ModelUsage, serialise

KEBAB_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def kebab_title(seed_text: str, max_len: int = 64) -> str:
    """Generate a kebab-case title slug from a seed string.

    Returns "" for empty or all-punctuation input; the caller is responsible
    for substituting an `untitled-{id8}` fallback.
    """
    lowered = seed_text.lower()
    kebab = KEBAB_NON_ALNUM.sub("-", lowered).strip("-")
    if not kebab:
        return ""
    if len(kebab) <= max_len:
        return kebab
    # Truncate without breaking in the middle of a word.
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
    """Build the markdown projection of a thread from its event log.

    Writes to ``Orchestrator/Live/{filename}`` and returns the path written.
    Idempotent: re-running with the same events produces a bytewise-identical
    file.
    """
    if not events:
        raise ValueError("Cannot project an empty event log.")

    first_user = next((e for e in events if e.event_type == "turn_user"), None)
    title_seed = first_user.payload.get("content", "")[:200] if first_user else ""
    id_short = thread_id.replace("-", "")[:8]
    title = kebab_title(title_seed) or f"untitled-{id_short}"

    created = datetime.fromisoformat(events[0].timestamp)
    updated = datetime.fromisoformat(events[-1].timestamp)

    turn_count = sum(
        1 for e in events if e.event_type in ("turn_user", "turn_assistant")
    )

    models_used = _aggregate_models(events)
    active_model = models_used[-1].id if models_used else "claude-sonnet-4-6"
    pinned_model = _extract_pinned_model(events)

    meta = ConversationFrontmatter(
        thread_id=thread_id,
        title=title,
        created=created,
        updated=updated,
        turn_count=turn_count,
        models_used=models_used,
        active_model=active_model,
        pinned_model=pinned_model,
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
    usage: list[ModelUsage] = []
    for e in events:
        if e.event_type != "turn_assistant":
            continue
        model_id: str = e.payload.get("model_id", "claude-sonnet-4-6")
        auth_mode: str = e.payload.get("auth_mode", "subscription")
        turn_num: int = e.payload.get("turn_number", 0)
        tokens: int = e.payload.get("estimated_tokens", 0)

        if usage and usage[-1].id == model_id and usage[-1].auth_mode == auth_mode:
            usage[-1].turns.append(turn_num)
            usage[-1].estimated_tokens += tokens
        else:
            usage.append(
                ModelUsage(
                    id=model_id,
                    auth_mode=auth_mode,  # type: ignore[arg-type]
                    turns=[turn_num],
                    estimated_tokens=tokens,
                )
            )
    return usage


def _extract_pinned_model(events: list[Event]) -> str | None:
    """Return the model from the latest thread_pin event, or None if unpinned."""
    pinned: str | None = None
    for e in events:
        if e.event_type == "thread_pin":
            pinned = e.payload.get("model") or None
    return pinned


def _render_body(events: list[Event]) -> str:
    """Render conversation events as markdown sections."""
    parts: list[str] = []
    for e in events:
        if e.event_type == "turn_user":
            turn_num: int = e.payload.get("turn_number", 0)
            content: str = e.payload.get("content", "")
            parts.append(f"## Turn {turn_num} — User\n{content}")
        elif e.event_type == "turn_assistant":
            turn_num = e.payload.get("turn_number", 0)
            content = e.payload.get("content", "")
            model_id: str = e.payload.get("model_id", "claude-sonnet-4-6")
            parts.append(f"## Turn {turn_num} — Assistant ({model_id})\n{content}")
    return "\n\n".join(parts)
