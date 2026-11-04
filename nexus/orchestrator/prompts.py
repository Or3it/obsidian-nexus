"""Prompt construction. Pure functions, no I/O.

Phase 1: trivial implementations.
Phase 2: handover-document insertion at model-boundary positions.
Phase 3: tier-budget context packing with retrieved archive + vault context.
Phase 3W: NEXUS_STYLE voice (all models), research preamble, project digest.
"""

from .projects.types import ProjectNote
from .types import NexusMessage, StyleProfile

# The Nexus voice — injected as a system message for all OpenRouter pool calls
# and via --append-system-prompt for all Claude Code subprocess calls.
NEXUS_STYLE = (
    "You are the Nexus. Answer the actual question first, directly. No filler, "
    "no flattery, no restating the user. If you don't know, say so plainly and "
    "say what would settle it. Verify before you claim — prefer checking over "
    "guessing. If the user's premise is wrong, say so and give the better path. "
    "Prefer concrete numbers, paths, and commands over abstractions. Keep short "
    "things short; go deep only when the problem is deep. When a next action "
    "exists, end with it."
)

# Appended after NEXUS_STYLE on rung-1 subprocess calls in research mode.
RESEARCH_PREAMBLE = (
    "Research thoroughly before answering. Use web search for anything current "
    "or uncertain. Cite sources with links. Distinguish what you verified from "
    "what you infer. If sources conflict, say so."
)

# One-line notice prepended to the reply when research mode falls back to a
# pool model without web access.
RESEARCH_FALLBACK_NOTICE = (
    "Note: web research was unavailable for this turn — answering from model "
    "knowledge without live web access.\n\n"
)


def build_system_prompt(style: StyleProfile) -> str:
    """Construct the system prompt for the active model.

    Phase 1: minimal — style profile only.
    Phase 3: retrieved archive context + vault notes get injected here.

    Keep this signature stable so Phase 3 doesn't require a refactor.
    """
    return (
        "You are an assistant working inside the user's Obsidian Nexus. "
        "The user's vault is the source of truth. Maintain continuity with "
        "prior turns; if you cannot, say so and ask.\n\n"
        f"Style profile for this thread:\n"
        f"  format: {style.format}\n"
        f"  verbosity: {style.verbosity}\n"
        f"  posture: {style.posture}\n"
    )


def build_project_context(
    notes: list[ProjectNote],
    max_projects: int = 25,
    summary_cap: int = 240,
) -> str:
    """Compact one-line-per-project digest for the system context (P3A-T07)."""
    if not notes:
        return ""
    lines = ["The user's projects (summary-level knowledge):"]
    for n in notes[:max_projects]:
        stack = ", ".join(n.stack[:5])
        summ = (n.summary or "").strip().replace("\n", " ")[:summary_cap]
        lines.append(f"- {n.name} [{n.category}; {stack}] — {summ}")
    return "\n".join(lines)


def build_message_history(
    history: list[NexusMessage],
    user_msg: str,
) -> list[NexusMessage]:
    """Stack prior turns + the current user message into a message list.

    Phase 1: simple linear stack.
    Phase 2: handover documents inserted at model-boundary positions.
    Phase 3: tier-budget packing decides what to keep / summarise / drop —
             but the output type stays the same; callers don't change.
    """
    messages = list(history)
    messages.append(NexusMessage(role="user", content=user_msg))
    return messages


def compose_request_messages(
    history: list[NexusMessage],
    user_msg: str,
    style: StyleProfile,
    project_context: str | None = None,
) -> list[NexusMessage]:
    """Compose system prompt + message history into the adapter's input.

    ``project_context`` (the P3A-T07 digest) is appended as its own system
    message, clearly delimited so the model treats it as reference material.
    """
    system_prompt = build_system_prompt(style)
    messages: list[NexusMessage] = []
    if system_prompt:
        messages.append(NexusMessage(role="system", content=system_prompt))
    if project_context:
        messages.append(
            NexusMessage(
                role="system",
                content=(
                    "--- Reference: the user's project catalogue ---\n"
                    f"{project_context}\n"
                    "--- End reference ---"
                ),
            )
        )
    messages.extend(build_message_history(history, user_msg))
    return messages
