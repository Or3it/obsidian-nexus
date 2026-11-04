# P1-T03 — Event Log (JSONL, Append-Only)

## Goal

Implement the JSONL append-only event log that is the canonical record of every conversation turn. Markdown is *projected* from this — never the reverse.

## Read first

- `shared/data-architecture.md` (writer rules, atomic appends)
- `shared/yaml-schemas.md` (event types and payload shapes)
- `shared/file-conventions.md` (filenames, IDs, encoding)

## Files to create or replace

```
orchestrator/vault/event_log.py
tests/test_event_log.py
```

## Implementation

```python
# orchestrator/vault/event_log.py

from pathlib import Path
from datetime import datetime, timezone
import json
import logging
import os
from dataclasses import dataclass
from typing import Literal, Any
import uuid_utils as uuid             # UUIDv7

log = logging.getLogger(__name__)

EventType = Literal["turn_user", "turn_assistant", "handover", "metadata_update"]

# When recovering from a corrupt tail, log a warning if we discard more than
# this many bytes — a torn write at the tail of one append should be a few
# hundred bytes at most. Anything larger suggests something else is wrong
# and the user should know.
TRUNCATE_WARN_BYTES = 4096


@dataclass
class Event:
    event_id: str
    timestamp: str                    # ISO 8601 with offset
    event_type: EventType
    schema_version: int
    payload: dict[str, Any]

    def to_json_line(self) -> str:
        return json.dumps({
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "payload": self.payload,
        }, ensure_ascii=False) + "\n"


class EventLog:
    """Per-thread append-only JSONL log."""

    def __init__(self, log_path: Path):
        self.path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: EventType, payload: dict[str, Any], schema_version: int = 2) -> Event:
        """Atomically append an event. Returns the event for caller use."""
        event = Event(
            event_id=str(uuid.uuid7()),
            timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            event_type=event_type,
            schema_version=schema_version,
            payload=payload,
        )
        line = event.to_json_line().encode("utf-8")
        # Atomic append: open with O_APPEND, single write call.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    def read_all(self) -> list[Event]:
        """
        Read all events. On the first corrupt line, truncate the tail and stop.

        This assumes the canonical failure mode is a single torn write at the
        end of the file (process killed mid-append). The PRD's "single writer
        per /Orchestrator/" invariant rules out concurrent-writer corruption.

        If we see corruption that is NOT at the tail (e.g., a valid line
        AFTER a corrupt line), that's a different bug — we log loudly and
        still truncate from the first corrupt point, because trusting
        anything after a corruption could silently reorder events.
        """
        if not self.path.exists():
            return []
        events: list[Event] = []
        first_corrupt_line: int | None = None
        saw_valid_after_corrupt = False

        with open(self.path, "r", encoding="utf-8") as f:
            for line_num, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if first_corrupt_line is not None:
                        # We saw a valid line AFTER a corrupt line. Don't trust it.
                        saw_valid_after_corrupt = True
                        continue
                    events.append(Event(
                        event_id=obj["event_id"],
                        timestamp=obj["timestamp"],
                        event_type=obj["event_type"],
                        schema_version=obj.get("schema_version", 2),
                        payload=obj["payload"],
                    ))
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
        """
        Truncate the file to the last good newline-terminated line.

        Implementation note: this currently reads the file into memory to
        find the cut point. For Phase 1 thread sizes (<1MB typical), that's
        fine. For threads that grow to tens of MB over months of use, this
        would cause a memory spike during recovery — but recovery is rare
        (one torn write per crash) and the alternative (byte-by-byte seek
        with rfind('\\n')) adds complexity that isn't worth it yet.
        Revisit if event log files routinely exceed ~10MB.
        """
        with open(self.path, "rb") as f:
            content = f.read()
        original_size = len(content)
        if last_good_line <= 0:
            kept = b""
        else:
            lines = content.split(b"\n")
            # Keep the first `last_good_line` lines, each terminated with \n.
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
```

## Behaviour rules

1. **Atomic append only.** Use `O_APPEND` so concurrent writers (we shouldn't have any, but defensive) don't interleave bytes.
2. **`fsync` after every write.** Phase 1 prioritizes durability over throughput; conversations are low-rate.
3. **Tolerant read.** A torn write at the tail of the file (last line incomplete) is recovered by truncating to the last good newline. Log this as a recovery event.
4. **Defensive against non-tail corruption.** If a valid line appears *after* a corrupt one — which shouldn't happen given the single-writer invariant — log loudly and still truncate from the first corrupt point. Silent acceptance would risk reordering events; loud truncation is the safe failure.
5. **Memory note on recovery.** The truncate path reads the whole file into memory. Fine for Phase 1; revisit if event logs exceed ~10MB.
6. **Never rewrite history.** `append` only. No edit/delete API in Phase 1. Corrections happen via additional events.
7. **Schema version pinning.** Every event carries `schema_version`. Phase 1 emits `2`. The reader accepts `>= 2`.

## Tests (`tests/test_event_log.py`)

Use `tmp_path` fixture for filesystem isolation.

- **Empty log:** `read_all()` on a non-existent file returns `[]`.
- **Single append:** `append("turn_user", {"content": "hi", "turn_number": 1})` then `read_all()` returns one event with matching payload.
- **Multiple appends, order preserved:** append three events; `read_all()` returns them in insertion order.
- **UUIDv7 monotonicity:** in a batch of appends within the same millisecond, `event_id`s sort lexically in insertion order. *(If `uuid_utils.uuid7()` doesn't guarantee monotonicity within a ms, document this and use `time.sleep_ms(1)` between writes in the test.)*
- **Torn write recovery (tail):** write a valid event, then write garbage bytes (no terminating newline). `read_all()` returns the one valid event and the file is truncated to just that event.
- **Non-tail corruption logged and truncated:** write valid event, corrupt line, valid event. `read_all()` returns only the first valid event, the file is truncated, and a warning is logged (capture via `caplog`).
- **Large truncation warns:** write a small valid event followed by ~10KB of garbage with no newline. `read_all()` returns the valid event and `caplog` captures a warning mentioning the discard size.
- **Concurrent appends (best-effort test):** two threads each append 100 events to the same log; `read_all()` returns 200 events with no corrupted lines and no lost events. *(If the runtime makes this flaky, mark `@pytest.mark.flaky` with a comment.)*
- **Unicode round-trip:** appending `{"content": "héllo 🌊 中文"}` round-trips bytewise identical.
- **`schema_version` defaulting:** `append(..., schema_version=2)` shows up in the file as `"schema_version": 2`. A version-3 entry read via `read_all()` is preserved as-is.

## Definition of done

- [ ] `event_log.py` matches the interface above.
- [ ] `pytest tests/test_event_log.py` passes (all ten tests).
- [ ] `ruff` and `mypy` clean.
- [ ] `read_all()` is O(n) in file size; no quadratic scans.
- [ ] Recovery paths emit log warnings — verifiable via `caplog` in tests.

## Definition of NOT done

- Don't implement projection to markdown yet — that's P1-T04.
- Don't implement event-type-specific helpers (e.g., `append_user_turn`); the bare `append` is enough for Phase 1. Higher-level helpers live in P1-T06.
- Don't add async — Phase 1 event log is sync. The orchestrator (P1-T06) wraps it in a threadpool if needed.
- Don't optimize `_truncate_after` for memory yet. The memory note is enough; revisit only when event logs grow large.
