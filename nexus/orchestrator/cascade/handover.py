"""Handover document writer.

Composes summariser output + YAML frontmatter and writes to
vault/Orchestrator/Handovers/{id_short8}_handover_{seq:02d}.md.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uuid_utils as uuid
import yaml

from ..types import NexusMessage, StyleProfile
from .summariser import Summariser
from .triggers import TriggerOutcome


async def generate_handover(
    *,
    summariser: Summariser,
    vault_root: Path,
    thread_id: str,
    sequence: int,
    from_model: str,
    to_model: str,
    trigger_outcome: TriggerOutcome,
    transcript: list[NexusMessage],
    style_profile: StyleProfile,
    full_history_filename: str,
    summariser_model_id: str,
) -> Path:
    """Generate the handover doc and write it to Handovers/. Returns the written file path."""
    handover_id = str(uuid.uuid7())
    id_short = thread_id.replace("-", "")[:8]
    triggered_at = datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")
    full_history_wiki_link = f"[[{full_history_filename.replace('.md', '')}]]"

    body = await summariser.summarise_for_handover(
        transcript=transcript,
        style_profile=style_profile,
        full_history_wiki_link=full_history_wiki_link,
    )

    frontmatter: dict[str, Any] = {
        "type": "handover",
        "schema_version": 2,
        "handover_id": handover_id,
        "thread_id": thread_id,
        "sequence": sequence,
        "from_model": from_model,
        "to_model": to_model,
        "trigger": trigger_outcome.trigger.value,
        "trigger_detail": trigger_outcome.detail,
        "triggered_at": triggered_at,
        "generated_by": "orchestrator",
        "summariser_model": summariser_model_id,
        "tokens_used": 0,
    }
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    markdown = f"---\n{fm_yaml}---\n\n{body}\n"

    handovers_dir = vault_root / "Orchestrator" / "Handovers"
    handovers_dir.mkdir(parents=True, exist_ok=True)
    out_path = handovers_dir / f"{id_short}_handover_{sequence:02d}.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


def load_handover(handover_path: Path) -> dict[str, Any]:
    """Parse a handover file. Returns {'frontmatter': dict, 'body': str}."""
    text = handover_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{handover_path} is missing YAML frontmatter")
    end = text.index("\n---\n", 4)
    fm_text = text[4:end]
    body = text[end + 5:]
    return {"frontmatter": yaml.safe_load(fm_text), "body": body.strip()}


def format_handover_for_prompt(handover: dict[str, Any]) -> str:
    """Format a parsed handover into the chunk injected into the next model's system prompt."""
    fm = handover["frontmatter"]
    body = handover["body"]
    return (
        "──── HANDOVER BRIEF ────\n"
        f"From: {fm['from_model']} → To: {fm['to_model']}\n"
        f"Trigger: {fm['trigger']} ({fm['trigger_detail']})\n"
        "\n"
        f"{body}\n"
        "────\n"
    )
