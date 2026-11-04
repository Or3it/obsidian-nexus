# P2a-T07 — Haiku Summariser + Handover Document Generation

## Goal

When the cascade fails over from one rung to the next, the new model needs context. The orchestrator generates a structured handover document using Haiku 4.5 (cheap, fast, Pro-covered) and writes it to `Orchestrator/Handovers/`. The next model is primed with this document instead of replaying the entire transcript.

## Read first

- `shared/yaml-schemas.md` (handover frontmatter schema)
- `shared/file-conventions.md` (filename pattern: `Handovers/{id-short8}_handover_{seq}.md`)
- `shared/data-architecture.md` (`/Orchestrator/Handovers/` must exist)
- P1-T06's prompts.py (we'll add a sibling module here, not modify that one)

## Files to create

```
nexus/orchestrator/cascade/handover.py
nexus/orchestrator/cascade/summariser.py
nexus/tests/test_handover.py
nexus/tests/test_summariser.py
nexus/tests/fixtures/handover_input.json     ← sample conversation transcript
nexus/tests/fixtures/handover_output.md      ← expected handover doc
```

## `nexus/orchestrator/cascade/summariser.py`

```python
"""
Haiku-based summariser. Used exclusively for generating handover docs.
Never appears in user-facing model selection.
"""

from ..adapters.anthropic import AnthropicAdapter
from ..types import NexusRequest, NexusMessage, StyleProfile


SUMMARISER_SYSTEM_PROMPT = """You are generating a handover document for a multi-model AI orchestration system. The active model is being switched. Produce a Markdown document with EXACTLY these sections in order:

## Summary
## Open Questions
## Last User Intent
## Recommended Continuation Style
## Full History Path

Constraints:
- Summary ≤ 500 words. Capture: what is being worked on, what has been concluded, what is contested, what the user's apparent goal is.
- Open Questions is a bulleted list of unresolved questions in the user's words.
- Last User Intent is ONE sentence describing what the user most recently asked for.
- Recommended Continuation Style is one paragraph guiding the next model on tone and posture.
- Full History Path is the wiki-link provided to you below — emit it verbatim.

Output ONLY the markdown document. No preamble, no closing remarks."""


class Summariser:
    """Wraps AnthropicAdapter with selection_hint='summariser' to route to Haiku."""

    def __init__(self, anthropic_adapter: AnthropicAdapter):
        self.adapter = anthropic_adapter

    async def summarise_for_handover(
        self,
        transcript: list[NexusMessage],
        style_profile: StyleProfile,
        full_history_wiki_link: str,
    ) -> str:
        """
        Generate the body of a handover document. Returns markdown.
        Does NOT include YAML frontmatter — that's the handover module's job.
        """
        prompt = self._build_prompt(transcript, style_profile, full_history_wiki_link)
        request = NexusRequest(
            thread_id="summariser",                # ephemeral
            messages=[
                NexusMessage(role="system", content=SUMMARISER_SYSTEM_PROMPT),
                NexusMessage(role="user", content=prompt),
            ],
            style_profile=StyleProfile(),
            selection_hint="summariser",
            stream=False,
        )
        response = await self.adapter.complete(request)
        return response.content

    def _build_prompt(
        self,
        transcript: list[NexusMessage],
        style_profile: StyleProfile,
        full_history_wiki_link: str,
    ) -> str:
        ...
```

## `nexus/orchestrator/cascade/handover.py`

```python
"""
Handover document writer. Composes summariser output + YAML frontmatter,
writes to vault under Orchestrator/Handovers/.
"""

from datetime import datetime, timezone
from pathlib import Path
import yaml
import uuid_utils as uuid

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
    """
    Generate the handover doc and write it to Handovers/.
    Returns the written file path.
    """
    handover_id = str(uuid.uuid7())
    id_short = thread_id.replace("-", "")[:8]
    triggered_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")

    full_history_wiki_link = f"[[{full_history_filename.replace('.md', '')}]]"

    body = await summariser.summarise_for_handover(
        transcript=transcript,
        style_profile=style_profile,
        full_history_wiki_link=full_history_wiki_link,
    )

    frontmatter = {
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
        "tokens_used": 0,                          # adapter doesn't expose per-call counts in sub mode
    }
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    markdown = f"---\n{fm_yaml}---\n\n{body}\n"

    handovers_dir = vault_root / "Orchestrator" / "Handovers"
    handovers_dir.mkdir(parents=True, exist_ok=True)
    out_path = handovers_dir / f"{id_short}_handover_{sequence:02d}.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


def load_handover(handover_path: Path) -> dict:
    """Parse a handover file. Returns {frontmatter: dict, body: str}."""
    text = handover_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{handover_path} is missing YAML frontmatter")
    end = text.index("\n---\n", 4)
    fm_text = text[4:end]
    body = text[end + 5:]
    return {"frontmatter": yaml.safe_load(fm_text), "body": body.strip()}


def format_handover_for_prompt(handover: dict) -> str:
    """
    Format a parsed handover into the chunk that goes into the next model's system prompt.
    """
    fm = handover["frontmatter"]
    body = handover["body"]
    return (
        f"──── HANDOVER BRIEF ────\n"
        f"From: {fm['from_model']} → To: {fm['to_model']}\n"
        f"Trigger: {fm['trigger']} ({fm['trigger_detail']})\n"
        f"\n"
        f"{body}\n"
        f"────\n"
    )
```

## Critical implementation notes

1. **The summariser uses Haiku via `selection_hint="summariser"`.** The Anthropic adapter routes accordingly (per P2a-T05). If Anthropic itself is the rung that's failing over, the summariser still tries Haiku first — Pro quota may be on Sonnet/Opus but Haiku might still have budget. If Haiku also fails, fall back to the local Ollama model as summariser (the cascade engine in P2a-T08 handles this).

2. **Sequence numbers are 1-indexed per thread.** First handover for a thread is `_handover_01.md`, second is `_handover_02.md`. The cascade engine knows the next number by reading the current `handovers: []` list in the thread's YAML.

3. **The transcript passed in is the FULL conversation, not just the last turn.** Haiku reads it and summarises down to the 500-word limit. Don't truncate before passing to the summariser; let Haiku decide what to compress.

4. **YAML frontmatter is deterministic.** Use `sort_keys=False` so fields appear in a stable order. This makes diffs readable.

5. **`format_handover_for_prompt` is what the cascade engine injects into the next model's context.** Keep it simple — the formatted string flows into the system prompt of the next adapter's `complete_stream` call. The orchestrator handles the actual wiring (P2a-T08, T09).

6. **Wiki-link points to the live conversation file.** The summariser doesn't know the filename — the cascade engine passes it in.

## Tests

`tests/test_summariser.py`:

- Mocked AnthropicAdapter returning a known summary string → `summarise_for_handover` returns it verbatim.
- The request sent to the adapter has `selection_hint="summariser"`.
- The system prompt sent to the adapter matches `SUMMARISER_SYSTEM_PROMPT`.

`tests/test_handover.py`:

- `generate_handover` writes a markdown file to `Handovers/{id8}_handover_{seq:02d}.md`.
- The file has valid YAML frontmatter conforming to the handover schema.
- The body is exactly what the (mocked) summariser returned.
- Sequence numbers format as `01`, `02`, `99` etc.
- `load_handover` round-trips: parses a file produced by `generate_handover` and returns matching frontmatter + body.
- `format_handover_for_prompt` produces a non-empty string containing the trigger class and the body.
- A handover file missing YAML frontmatter raises `ValueError` on `load_handover`.

## Definition of done

- [ ] `cascade/summariser.py` and `cascade/handover.py` exist.
- [ ] All tests pass.
- [ ] Manual test: with the orchestrator's Anthropic adapter live, generate a real handover from a fixture transcript. The output file is human-readable and has the five required sections.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't wire to the cascade engine — P2a-T08.
- Don't implement the local-Ollama summariser fallback — P2a-T08 owns that. This module just calls Haiku.
- Don't expose handover docs to the UI yet — the plugin showing a "handover happened" toast is P2a-T10.
- Don't link the handover into the thread's YAML `handovers: []` list yet — the cascade engine appends to it when it writes the doc.
