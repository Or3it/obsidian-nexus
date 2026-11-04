"""Phase 1 end-to-end integration tests.

Each test tagged @pytest.mark.real_claude requires:
  - Claude Code installed and authenticated (claude --version succeeds)
  - A Claude Pro subscription active in Claude Code's session
  - Run via: pytest tests/integration/ --real-claude

Tests 1 and 2 are verified outside this file (unit suite + manual plugin check).
Tests 3-7 are automated here.
"""
import json
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

PORT = 7778
BASE_URL = f"http://127.0.0.1:{PORT}"

CASCADE_YAML = textwrap.dedent("""\
    schema_version: 2
    rungs:
      - position: 1
        adapter: anthropic
        auth_mode: subscription
        use_ruflo: false
        models:
          default: claude-sonnet-4-6
          premium: claude-opus-4-7
          summariser: claude-haiku-4-5
    jurisdictions: {}
""")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Fresh vault root. Writes the minimal cascade.yaml needed to start the orchestrator."""
    v = tmp_path / "vault"
    v.mkdir()
    config_dir = v / "Orchestrator" / "Config"
    config_dir.mkdir(parents=True)
    (config_dir / "cascade.yaml").write_text(CASCADE_YAML, encoding="utf-8")
    return v


@pytest.fixture()
def orchestrator_server(vault: Path) -> Iterator[str]:
    """Start the orchestrator, wait for /health 200, yield base URL, then terminate."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "--vault", str(vault), "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_healthy(proc)
        yield BASE_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_healthy(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise RuntimeError(f"Orchestrator exited prematurely: {stderr[:500]}")
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Orchestrator did not become healthy within timeout")


def _auth_headers(vault: Path) -> dict[str, str]:
    token = (vault / "Orchestrator" / "Config" / "auth.token").read_text().strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _parse_sse(lines: Iterator[str]) -> list[dict[str, str]]:
    """Minimal SSE parser: returns list of {event, data} dicts."""
    events: list[dict[str, str]] = []
    buf: list[str] = []
    for line in lines:
        if line == "":
            if buf:
                event, data_parts = "message", []
                for raw in buf:
                    if raw.startswith("event:"):
                        event = raw[6:].strip()
                    elif raw.startswith("data:"):
                        data_parts.append(raw[5:].strip())
                events.append({"event": event, "data": "\n".join(data_parts)})
                buf = []
        else:
            buf.append(line)
    return events


# ---------------------------------------------------------------------------
# AC3: end-to-end streamed turn
# ---------------------------------------------------------------------------


@pytest.mark.real_claude
def test_e2e_streamed_turn(vault: Path, orchestrator_server: str) -> None:
    """Typing a message streams a real Sonnet 4.6 response via Claude Code."""
    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Say 'phase one works' and nothing else."},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    event_types = {e["event"] for e in events}
    assert "thread" in event_types, f"No 'thread' event. Got: {event_types}"
    assert "token" in event_types, f"No 'token' event. Got: {event_types}"
    assert "done" in event_types, f"No 'done' event. Got: {event_types}"

    text = "".join(
        json.loads(e["data"])["text"]
        for e in events
        if e["event"] == "token"
    )
    assert "phase one works" in text.lower(), f"Expected phrase not found in: {text!r}"


# ---------------------------------------------------------------------------
# AC4: vault persistence
# ---------------------------------------------------------------------------


@pytest.mark.real_claude
def test_vault_persistence(vault: Path, orchestrator_server: str) -> None:
    """After a turn, Live/ has a markdown file with valid YAML frontmatter."""
    import frontmatter  # type: ignore[import-untyped]

    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Reply with exactly: persistence check"},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    # Thread ID comes from the 'thread' SSE event
    thread_id = None
    for e in events:
        if e["event"] == "thread":
            thread_id = json.loads(e["data"])["thread_id"]
            break
    assert thread_id is not None, "No thread event received"

    live_dir = vault / "Orchestrator" / "Live"
    md_files = list(live_dir.glob("*.md"))
    assert md_files, f"No markdown files in Live/ after turn. thread_id={thread_id}"

    md_path = md_files[0]
    post = frontmatter.load(str(md_path))
    fm = post.metadata

    # Phase 1 minimum frontmatter fields (from yaml-schemas.md)
    assert "thread_id" in fm, f"Missing thread_id in frontmatter: {fm}"
    assert "status" in fm, f"Missing status in frontmatter: {fm}"
    assert "models_used" in fm, f"Missing models_used in frontmatter: {fm}"
    assert fm["thread_id"] == thread_id, (
        f"Frontmatter thread_id {fm['thread_id']!r} != expected {thread_id!r}"
    )


# ---------------------------------------------------------------------------
# AC5: event log integrity
# ---------------------------------------------------------------------------


@pytest.mark.real_claude
def test_event_log_integrity(vault: Path, orchestrator_server: str) -> None:
    """After a turn, Logs/ has a JSONL with one turn_user and one turn_assistant."""
    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Reply with exactly: log check"},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    thread_id = None
    for e in events:
        if e["event"] == "thread":
            thread_id = json.loads(e["data"])["thread_id"]
            break
    assert thread_id is not None

    logs_dir = vault / "Orchestrator" / "Logs"
    jsonl_files = list(logs_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL log files found after turn"

    # Find the log for this thread (16-char prefix of UUID without dashes)
    id_short = thread_id.replace("-", "")[:16]
    log_path = logs_dir / f"{id_short}.jsonl"
    assert log_path.exists(), (
        f"Expected log at {log_path}. Found: {[f.name for f in jsonl_files]}"
    )

    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    event_types = [line["event_type"] for line in lines]
    assert "turn_user" in event_types, f"No turn_user in log: {event_types}"
    assert "turn_assistant" in event_types, f"No turn_assistant in log: {event_types}"

    # Order must be user before assistant
    user_idx = event_types.index("turn_user")
    asst_idx = event_types.index("turn_assistant")
    assert user_idx < asst_idx, f"turn_user ({user_idx}) must precede turn_assistant ({asst_idx})"

    # User payload must match what we sent
    user_event = lines[user_idx]
    assert "log check" in user_event["payload"]["content"]


# ---------------------------------------------------------------------------
# AC6: restart survival
# ---------------------------------------------------------------------------


@pytest.mark.real_claude
def test_restart_survival(vault: Path) -> None:
    """Stopping and restarting the orchestrator preserves thread history via JSONL."""
    # --- First run: do a turn ---
    proc1 = subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "--vault", str(vault), "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_healthy(proc1)
        headers = _auth_headers(vault)
        with httpx.Client() as client, client.stream(
            "POST",
            f"{BASE_URL}/turn",
            headers=headers,
            json={"user_message": "Reply with exactly: restart check"},
            timeout=120.0,
        ) as r:
            assert r.status_code == 200
            events = _parse_sse(r.iter_lines())
    finally:
        proc1.terminate()
        try:
            proc1.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc1.kill()

    thread_id = None
    for e in events:
        if e["event"] == "thread":
            thread_id = json.loads(e["data"])["thread_id"]
            break
    assert thread_id is not None

    # Brief pause to ensure OS releases the port
    time.sleep(1.0)

    # --- Second run: verify thread is readable ---
    proc2 = subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "--vault", str(vault), "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_healthy(proc2)
        headers = _auth_headers(vault)
        with httpx.Client() as client:
            r = client.get(
                f"{BASE_URL}/threads/{thread_id}",
                headers=headers,
                timeout=10.0,
            )
        assert r.status_code == 200, f"GET /threads/{thread_id} → {r.status_code}"
        thread_events = r.json()
        assert isinstance(thread_events, list), f"Expected list, got: {type(thread_events)}"
        assert len(thread_events) >= 2, f"Expected ≥2 events, got {len(thread_events)}"
        event_types = [e["event_type"] for e in thread_events]
        assert "turn_user" in event_types, f"turn_user missing after restart: {event_types}"
        assert "turn_assistant" in event_types, (
            f"turn_assistant missing after restart: {event_types}"
        )
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()


# ---------------------------------------------------------------------------
# AC7: Ruflo-optional (use_ruflo: false)
# ---------------------------------------------------------------------------


@pytest.mark.real_claude
def test_use_ruflo_false_fallback(vault: Path, orchestrator_server: str) -> None:
    """use_ruflo: false invokes Claude Code directly; response streams successfully.

    This is the same path exercised by all other tests (cascade.yaml is written
    with use_ruflo: false in the vault fixture). This test asserts it explicitly
    by verifying the health endpoint reports the claude binary as healthy, and
    that the turn succeeds end-to-end — confirming no Ruflo dependency.
    """
    headers_no_ct = {"Authorization": _auth_headers(vault)["Authorization"]}
    r = httpx.get(f"{orchestrator_server}/health", headers=headers_no_ct, timeout=10.0)
    assert r.status_code == 200
    health = r.json()
    assert health["healthy"] is True, f"Health check failed: {health}"

    # Confirm cascade.yaml on disk has use_ruflo: false
    cascade_text = (vault / "Orchestrator" / "Config" / "cascade.yaml").read_text()
    assert "use_ruflo: false" in cascade_text, "cascade.yaml does not have use_ruflo: false"

    # End-to-end turn via direct Claude Code invocation
    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Say 'ruflo not required' and nothing else."},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    text = "".join(
        json.loads(e["data"])["text"]
        for e in events
        if e["event"] == "token"
    )
    assert "ruflo not required" in text.lower(), f"Expected phrase not in: {text!r}"
