"""Summarize an approved project candidate via the Claude Code subprocess.

Reads README contents HERE (post-approval, capped at 6000 chars) and feeds
the README + file tree to Sonnet. Returns a fully-formed ProjectNote.

Never uses the paid Anthropic SDK path — subprocess only (Pro subscription).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .types import ProjectCandidate, ProjectNote

log = logging.getLogger(__name__)

_README_CAP = 6_000
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

_VALID_CATEGORIES = frozenset(
    {"trading", "ai-tooling", "scraping", "worship", "other"}
)

_SUBPROCESS_CMD = [
    "claude",
    "--model",
    "claude-sonnet-4-6",
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
]


@dataclass
class ProjectSummary:
    summary: str
    stack: list[str]
    category: str


async def summarize(
    candidate: ProjectCandidate,
    timeout: int = 120,
) -> ProjectNote:
    """Generate a summary for *candidate* via the Claude Code subprocess.

    Returns a ProjectNote with all fields populated.  ``discovered_at`` is
    set to now; the store's write function preserves it on re-index.
    """
    readme = _read_readme(candidate, cap=_README_CAP)
    prompt = _build_prompt(candidate, readme)
    raw = await _run_subprocess(prompt, timeout=timeout)
    parsed = _parse(raw, candidate)
    now = datetime.now(tz=UTC)
    return ProjectNote(
        name=candidate.name,
        slug=candidate.slug,
        path=candidate.path,
        stack=parsed.stack,
        status=candidate.inferred_status,
        category=parsed.category,
        run_command=None,
        preview=None,
        last_indexed=now,
        discovered_at=now,
        summary=parsed.summary,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_readme(candidate: ProjectCandidate, cap: int = _README_CAP) -> str:
    if not candidate.readme_path:
        return ""
    try:
        text = Path(candidate.readme_path).read_text(encoding="utf-8", errors="replace")
        return text[:cap]
    except OSError:
        return ""


def _build_prompt(candidate: ProjectCandidate, readme: str) -> str:
    tree_text = "\n".join(candidate.file_tree[:80])
    readme_section = readme if readme else "(no README)"
    return (
        "You are cataloguing a software project for a personal dashboard.\n"
        "Given the README and file tree, respond with ONLY a JSON object, no prose:\n"
        '{\n'
        '  "summary": "<2-3 sentence plain-English description of what this project does>",\n'
        '  "stack": ["<key technologies/frameworks, lowercase>"],\n'
        '  "category": "<one of: trading, ai-tooling, scraping, worship, other>"\n'
        "}\n\n"
        f"Project name: {candidate.name}\n"
        f"File tree:\n{tree_text}\n\n"
        f"README:\n{readme_section}"
    )


async def _run_subprocess(prompt: str, timeout: int = 120) -> str:
    """Invoke `claude --print` with *prompt* on stdin; return the full text response."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_SUBPROCESS_CMD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("claude binary not found — returning empty response")
        return ""

    assert proc.stdin is not None
    assert proc.stdout is not None

    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    chunks: list[str] = []
    try:
        while True:
            try:
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=float(timeout),
                )
            except TimeoutError:
                log.warning("claude subprocess timed out after %ds", timeout)
                break

            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            text = _extract_text(line)
            if text is not None:
                chunks.append(text)

        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except TimeoutError:
        pass
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()

    return "".join(chunks)


def _extract_text(line: str) -> str | None:
    """Parse one stream-json line; return text content or None to skip."""
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    msg_type = obj.get("type")
    if msg_type == "assistant":
        blocks = obj.get("message", {}).get("content", [])
        texts = [b["text"] for b in blocks if b.get("type") == "text" and "text" in b]
        return "".join(texts) if texts else None
    if msg_type == "result":
        return None  # stream done — no text to emit
    return None


def _parse(raw: str, candidate: ProjectCandidate) -> ProjectSummary:
    """Parse the model output into structured fields; fall back gracefully."""
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        data = json.loads(cleaned)
        summary = str(data.get("summary", "")).strip()
        stack = [str(s).lower() for s in data.get("stack", []) if s]
        category = str(data.get("category", "other")).lower()
        if category not in _VALID_CATEGORIES:
            category = "other"
        if not summary:
            raise ValueError("empty summary")
        return ProjectSummary(summary=summary, stack=stack, category=category)
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        log.warning(
            "Failed to parse summarizer JSON for %r — using fallback", candidate.name
        )
        lang_str = ", ".join(candidate.languages) if candidate.languages else "unknown technology"
        return ProjectSummary(
            summary=f"{candidate.name}: project using {lang_str}.",
            stack=list(candidate.languages),
            category="other",
        )
