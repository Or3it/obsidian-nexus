"""Append-only JSONL event log for per-thread conversation events.

JSONL is the canonical record. Markdown files are projections from this;
if they disagree, JSONL wins and markdown is rebuilt.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import uuid_utils as uuid

log = logging.getLogger(__name__)

EventType = Literal[
    "turn_user",
    "turn_assistant",
    "handover",
    "metadata_update",
    "thread_pin",
    "turn_classified",
]

# When recovering from a corrupt tail, warn if we discard more than this many
# bytes. A torn write at the tail of one append should be a few hundred bytes
# at most; anything larger suggests something else went wrong.
TRUNCATE_WARN_BYTES = 4096


@dataclass
class Event:
    event_id: str
    timestamp: str  # ISO 8601 with offset
    event_type: EventType
    schema_version: int
    payload: dict[str, Any]

    def to_json_line(self) -> str:
        return (
            json.dumps(
                {
                    "event_id": self.event_id,
                    "timestamp": self.timestamp,
                    "event_type": self.event_type,
                    "schema_version": self.schema_version,
                    "payload": self.payload,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


class EventLog:
    """Per-thread append-only JSONL log."""

    def __init__(self, log_path: Path) -> None:
        self.path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        event_type: EventType,
        payload: dict[str, Any],
        schema_version: int = 2,
    ) -> Event:
        """Atomically append one event. Returns the event for caller use."""
        event = Event(
            event_id=str(uuid.uuid7()),
            timestamp=datetime.now(UTC).astimezone().isoformat(
                timespec="milliseconds"
            ),
            event_type=event_type,
            schema_version=schema_version,
            payload=payload,
        )
        line = event.to_json_line().encode("utf-8")
        # O_APPEND makes each write atomic at the OS level so concurrent
        # writers (which the single-writer invariant forbids, but defensive)
        # cannot interleave bytes.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    def read_all(self) -> list[Event]:
        """Read all events, truncating and logging on corruption.

        Canonical failure mode: a torn write at the tail of the file (process
        killed mid-append). We truncate to the last good newline-terminated
        line and log a recovery warning.

        If a valid line appears *after* a corrupt line — which violates the
        single-writer invariant — we log loudly and still truncate from the
        first corrupt point. Trusting events after a corruption could silently
        reorder history; loud truncation is the safe failure mode.
        """
        if not self.path.exists():
            return []

        events: list[Event] = []
        first_corrupt_line: int | None = None
        saw_valid_after_corrupt = False

        with open(self.path, encoding="utf-8") as f:
            for line_num, raw in enumerate(f, 1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    if first_corrupt_line is not None:
                        saw_valid_after_corrupt = True
                        continue
                    events.append(
                        Event(
                            event_id=obj["event_id"],
                            timestamp=obj["timestamp"],
                            event_type=obj["event_type"],
                            schema_version=obj.get("schema_version", 2),
                            payload=obj["payload"],
                        )
                    )
                except (json.JSONDecodeError, KeyError):
                    if first_corrupt_line is None:
                        first_corrupt_line = line_num

        if first_corrupt_line is not None:
            if saw_valid_after_corrupt:
                log.warning(
                    "Event log %s: corruption is NOT at the tail "
                    "(valid line followed a corrupt one). Truncating from "
                    "line %d anyway, but this is unexpected — investigate.",
                    self.path,
                    first_corrupt_line,
                )
            self._truncate_after(first_corrupt_line - 1)

        return events

    def _truncate_after(self, last_good_line: int) -> None:
        """Truncate the file to the last good newline-terminated line.

        Reads the whole file into memory to find the cut point. Fine for
        Phase 1 (typical <1 MB per thread); revisit if event logs routinely
        exceed ~10 MB (a byte-by-byte rfind would avoid the memory spike).
        """
        with open(self.path, "rb") as f:
            content = f.read()

        original_size = len(content)

        if last_good_line <= 0:
            kept = b""
        else:
            lines = content.split(b"\n")
            kept = b"\n".join(lines[:last_good_line]) + b"\n"

        discarded_bytes = original_size - len(kept)
        if discarded_bytes > TRUNCATE_WARN_BYTES:
            log.warning(
                "Event log %s: truncating %d bytes (> %d threshold). "
                "A torn write at the tail should be a few hundred bytes; "
                "this is large enough to suggest something else went wrong.",
                self.path,
                discarded_bytes,
                TRUNCATE_WARN_BYTES,
            )

        with open(self.path, "wb") as f:
            f.write(kept)
