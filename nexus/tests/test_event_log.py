"""Tests for orchestrator/vault/event_log.py."""

import concurrent.futures
import json
import logging
from pathlib import Path

import pytest

from orchestrator.vault.event_log import TRUNCATE_WARN_BYTES, EventLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_log(tmp_path: Path, name: str = "test.jsonl") -> EventLog:
    return EventLog(tmp_path / name)


# ---------------------------------------------------------------------------
# 1. Empty log
# ---------------------------------------------------------------------------


def test_read_all_nonexistent_returns_empty(tmp_path: Path) -> None:
    el = make_log(tmp_path)
    assert el.read_all() == []


# ---------------------------------------------------------------------------
# 2. Single append
# ---------------------------------------------------------------------------


def test_single_append_round_trips(tmp_path: Path) -> None:
    el = make_log(tmp_path)
    written = el.append("turn_user", {"content": "hi", "turn_number": 1})
    events = el.read_all()
    assert len(events) == 1
    e = events[0]
    assert e.event_id == written.event_id
    assert e.event_type == "turn_user"
    assert e.payload == {"content": "hi", "turn_number": 1}
    assert e.schema_version == 2


# ---------------------------------------------------------------------------
# 3. Multiple appends — insertion order preserved
# ---------------------------------------------------------------------------


def test_multiple_appends_preserve_order(tmp_path: Path) -> None:
    el = make_log(tmp_path)
    ids = []
    for i in range(3):
        ev = el.append("turn_user", {"content": f"msg-{i}", "turn_number": i})
        ids.append(ev.event_id)
    events = el.read_all()
    assert len(events) == 3
    assert [e.event_id for e in events] == ids
    assert [e.payload["turn_number"] for e in events] == [0, 1, 2]


# ---------------------------------------------------------------------------
# 4. UUIDv7 monotonicity
# ---------------------------------------------------------------------------


def test_uuid7_monotonicity(tmp_path: Path) -> None:
    """UUIDv7 event_ids must sort lexically in insertion order."""
    el = make_log(tmp_path)
    events = [
        el.append("turn_user", {"content": f"m{i}", "turn_number": i})
        for i in range(50)
    ]
    ids = [e.event_id for e in events]
    # uuid_utils.uuid7() uses a monotonic counter so this holds even for
    # rapid in-process calls within the same millisecond.
    assert ids == sorted(ids), (
        "uuid7 ids were not monotonically increasing — "
        "uuid_utils may not guarantee sub-ms monotonicity on this platform"
    )


# ---------------------------------------------------------------------------
# 5. Torn write recovery (tail)
# ---------------------------------------------------------------------------


def test_torn_write_recovery_tail(tmp_path: Path) -> None:
    """A truncated JSON blob at the tail is discarded; file is repaired."""
    el = make_log(tmp_path)
    good = el.append("turn_user", {"content": "good", "turn_number": 1})

    # Simulate a torn write: incomplete JSON with no terminating newline
    with open(el.path, "ab") as f:
        f.write(b'{"broken": ')

    events = el.read_all()
    assert len(events) == 1
    assert events[0].event_id == good.event_id

    # File must be repaired to exactly one line
    content = el.path.read_bytes()
    assert content.count(b"\n") == 1
    assert content.endswith(b"\n")


# ---------------------------------------------------------------------------
# 6. Non-tail corruption — logged and truncated
# ---------------------------------------------------------------------------


def test_non_tail_corruption_logged_and_truncated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt middle line: only pre-corruption events returned; warning logged."""
    el = make_log(tmp_path)
    good1 = el.append("turn_user", {"content": "good1", "turn_number": 1})

    # Insert a corrupt line directly (simulates non-tail corruption)
    with open(el.path, "ab") as f:
        f.write(b'{"broken":\n')

    el.append("turn_user", {"content": "good2", "turn_number": 2})

    with caplog.at_level(logging.WARNING, logger="orchestrator.vault.event_log"):
        events = el.read_all()

    assert len(events) == 1
    assert events[0].event_id == good1.event_id
    assert "NOT at the tail" in caplog.text


# ---------------------------------------------------------------------------
# 7. Large truncation warns
# ---------------------------------------------------------------------------


def test_large_truncation_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Discarding > TRUNCATE_WARN_BYTES bytes triggers a size warning."""
    el = make_log(tmp_path)
    el.append("turn_user", {"content": "ok", "turn_number": 1})

    # Write garbage well in excess of the warning threshold, no newline
    garbage = b"X" * (TRUNCATE_WARN_BYTES + 1024)
    with open(el.path, "ab") as f:
        f.write(garbage)

    with caplog.at_level(logging.WARNING, logger="orchestrator.vault.event_log"):
        events = el.read_all()

    assert len(events) == 1
    assert any("truncating" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 8. Concurrent appends
# ---------------------------------------------------------------------------


def test_concurrent_appends(tmp_path: Path) -> None:
    """Two threads each append 100 events; all 200 survive uncorrupted."""
    el = make_log(tmp_path)

    def write_n(n: int) -> None:
        for i in range(n):
            el.append("turn_user", {"content": f"msg-{i}", "turn_number": i})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write_n, 100) for _ in range(2)]
        for fut in futures:
            fut.result()

    events = el.read_all()
    assert len(events) == 200
    # All lines must be valid JSON — no corruption
    raw_lines = [
        line
        for line in el.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(raw_lines) == 200
    for line in raw_lines:
        json.loads(line)  # must not raise


# ---------------------------------------------------------------------------
# 9. Unicode round-trip
# ---------------------------------------------------------------------------


def test_unicode_round_trip(tmp_path: Path) -> None:
    """Non-ASCII content survives a write/read cycle bytewise identical."""
    content = "héllo 🌊 中文"
    el = make_log(tmp_path)
    el.append("turn_user", {"content": content, "turn_number": 1})
    events = el.read_all()
    assert len(events) == 1
    assert events[0].payload["content"] == content


# ---------------------------------------------------------------------------
# 10. schema_version defaulting and forward-compat
# ---------------------------------------------------------------------------


def test_schema_version_written_and_read(tmp_path: Path) -> None:
    el = make_log(tmp_path)
    el.append("turn_user", {"content": "x", "turn_number": 1}, schema_version=2)
    raw = el.path.read_text(encoding="utf-8")
    obj = json.loads(raw.strip())
    assert obj["schema_version"] == 2


def test_future_schema_version_preserved(tmp_path: Path) -> None:
    """A version-3 entry written directly is read back with schema_version=3."""
    el = make_log(tmp_path)
    # Write a future-schema event directly
    future_event: dict[str, object] = {
        "event_id": "00000000-0000-7000-8000-000000000001",
        "timestamp": "2026-05-15T00:00:00.000+00:00",
        "event_type": "turn_user",
        "schema_version": 3,
        "payload": {"content": "future", "turn_number": 1},
    }
    with open(el.path, "a", encoding="utf-8") as f:
        f.write(json.dumps(future_event) + "\n")

    events = el.read_all()
    assert len(events) == 1
    assert events[0].schema_version == 3
    assert events[0].payload["content"] == "future"
