"""Cascade event types emitted by CascadeEngine.complete_stream."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..types import Token


@dataclass
class TokenEvent:
    token: Token


@dataclass
class HandoverEvent:
    from_rung: int
    to_rung: int
    from_model: str
    to_model: str
    handover_path: Path
    trigger: str
    # accurate failure class: "model_unavailable" / "rate_limited" / "quota_exhausted"
    reason: str = ""


@dataclass
class ErrorEvent:
    message: str
    terminal: bool


@dataclass
class ToolCallEvent:
    """Emitted when the SDK path invokes a tool. Plugin uses this to show a search indicator."""

    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultEvent:
    """Emitted after a tool call completes with its formatted result."""

    name: str
    content: str


@dataclass
class ClassificationEvent:
    """Emitted once per turn with the task classifier's decision.

    The orchestrator handles this event to append a turn_classified JSONL entry.
    ``mode`` is the resolved turn mode (light/hard/research) after auto-mapping —
    Phase 3W mode-based routing supersedes the old route_by_role switch.
    """

    task_class: str
    confidence: str
    reason: str
    backend: str
    mode: str = "auto"


@dataclass
class MetaEvent:
    """Emitted once per turn after model selection: who is answering, in what mode.

    The server streams this as `event: meta` so UIs can display routing per turn.
    """

    mode: str
    requested_mode: str
    model: str


CascadeEvent = (
    TokenEvent | HandoverEvent | ErrorEvent | ToolCallEvent | ToolResultEvent
    | ClassificationEvent | MetaEvent
)
