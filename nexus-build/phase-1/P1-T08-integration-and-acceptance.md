# P1-T08 — Integration + Acceptance

## Goal

Verify all seven Phase 1 acceptance criteria end-to-end against a real Sonnet 4.6 session. This ticket is the final gate before declaring Phase 1 done and moving to Phase 2.

## Read first

- `phase-1/00-OVERVIEW.md` (the seven acceptance criteria)
- `CLAUDE.md` (rules — particularly #4 about JSONL as source of truth)

## Files to create or replace

```
tests/integration/test_phase1_e2e.py
tests/integration/conftest.py
tests/integration/README.md           ← how to run integration tests
scripts/smoke.sh                      ← optional convenience wrapper
README.md                             ← update with "How to verify Phase 1"
docs/phase-1-acceptance-report.md     ← filled-out checklist + screenshots
```

## Integration test strategy

Phase 1 integration tests need a real Claude Code installation and a real Pro session. They are **not part of `pytest tests/`** (which is mocked unit tests). They live in `tests/integration/` and are run on demand:

```
pytest tests/integration/ --vault /tmp/nexus-itest-vault --real-claude
```

The `--real-claude` flag is a custom pytest option; tests gated on it skip by default.

## The seven acceptance tests

`tests/integration/test_phase1_e2e.py`:

```python
import asyncio
import pytest
from pathlib import Path
import subprocess
import httpx
import time
import json

@pytest.fixture
def vault(tmp_path):
    """Fresh vault root for each test."""
    v = tmp_path / "vault"
    v.mkdir()
    return v

@pytest.fixture
def orchestrator_server(vault):
    """Start the orchestrator subprocess, wait for it to be healthy, tear down."""
    proc = subprocess.Popen(
        ["python", "-m", "orchestrator", "--vault", str(vault), "--port", "7778"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Wait up to 10s for /health to return 200.
    for _ in range(20):
        try:
            r = httpx.get("http://127.0.0.1:7778/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Orchestrator did not become healthy")
    yield "http://127.0.0.1:7778"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

def _auth_headers(vault: Path) -> dict[str, str]:
    token = (vault / "Orchestrator" / "Config" / "auth.token").read_text().strip()
    return {"Authorization": f"Bearer {token}"}

# Test 1: pytest tests/ passes — verified separately as part of CI; no test here.

# Test 2: Plugin renders — verified manually; documented in acceptance report.

# Test 3: End-to-end streamed turn.
@pytest.mark.real_claude
def test_e2e_streamed_turn(vault, orchestrator_server):
    base = orchestrator_server
    headers = _auth_headers(vault) | {"Content-Type": "application/json"}
    with httpx.Client() as client, client.stream(
        "POST", f"{base}/turn",
        headers=headers,
        json={"user_message": "Say 'phase one works' and nothing else."},
        timeout=60.0,
    ) as r:
        assert r.status_code == 200
        events = list(_parse_sse(r.iter_lines()))
    assert any(e["event"] == "thread" for e in events)
    assert any(e["event"] == "token" for e in events)
    assert any(e["event"] == "done" for e in events)
    text = "".join(json.loads(e["data"])["text"] for e in events if e["event"] == "token")
    assert "phase one works" in text.lower()

# Test 4: Vault persistence — frontmatter validation.
@pytest.mark.real_claude
def test_vault_persistence(vault, orchestrator_server):
    # Run a turn, then check Live/ has a markdown file with valid frontmatter.
    ...

# Test 5: Event log integrity.
@pytest.mark.real_claude
def test_event_log_integrity(vault, orchestrator_server):
    # After a turn, Logs/{id8}.jsonl has exactly one turn_user and one turn_assistant.
    ...

# Test 6: Restart survival.
@pytest.mark.real_claude
def test_restart_survival(vault):
    # Start orchestrator, send turn, stop, restart, GET /threads/{id} returns prior events.
    ...

# Test 7: Ruflo-optional.
@pytest.mark.real_claude
def test_use_ruflo_false_fallback(vault):
    # Configure cascade.yaml with use_ruflo: false, start orchestrator, send turn,
    # verify response streamed successfully (Claude Code invoked directly).
    ...

def _parse_sse(lines):
    """Tiny SSE parser for testing."""
    buf = []
    for line in lines:
        if line == "":
            if buf:
                event, data = "message", []
                for l in buf:
                    if l.startswith("event:"): event = l[6:].strip()
                    elif l.startswith("data:"): data.append(l[5:].strip())
                yield {"event": event, "data": "\n".join(data)}
                buf = []
        else:
            buf.append(line)
```

## Manual checklist (`docs/phase-1-acceptance-report.md`)

Fill out and commit this file when Phase 1 is verified end-to-end.

```markdown
# Phase 1 Acceptance Report

Date: ____
Tester: ____
Environment: macOS / Linux / Windows ____ | Python ____ | Node ____

## Seven acceptance criteria

- [ ] **1. pytest tests/ passes.** `__ tests passed in __s`.
- [ ] **2. Plugin renders.** Installed in vault at `__`; "Nexus Chat" view available. *Screenshot:* `docs/screenshots/phase-1-chat-view.png`
- [ ] **3. End-to-end streamed turn.** Sent "Say 'phase one works'..."; streamed response received. *Tail of orchestrator log attached.*
- [ ] **4. Vault persistence.** File `Orchestrator/Live/____.md` exists; frontmatter validated. *Frontmatter dump attached.*
- [ ] **5. Event log integrity.** File `Orchestrator/Logs/____.jsonl` has two events. *Cat output attached.*
- [ ] **6. Restart survival.** Stopped orchestrator, restarted, `GET /threads/{id}` returns prior events. *Curl output attached.*
- [ ] **7. Ruflo-optional.** Set `use_ruflo: false`, ran a turn, succeeded. *Discussion of any behavior differences observed:*

## Drift discovered (from OPEN_QUESTIONS.md)

(paste contents here)

## Soak observation

Ran the chat view for __ minutes with __ messages. Observed:
- Latency P50 / P95: __ / __ ms.
- Any crashes or unhandled errors: __
- Memory / CPU behaviour: __

## Sign-off

Phase 1 is ready for Phase 2: ☐ Yes / ☐ No, blocking issues: __
```

## Definition of done

- [ ] `tests/integration/test_phase1_e2e.py` exists and all `--real-claude`-gated tests pass against a live Sonnet session.
- [ ] `docs/phase-1-acceptance-report.md` is filled in with checkboxes and attached evidence.
- [ ] `README.md` has a "How to verify Phase 1" section pointing to `scripts/smoke.sh` or the manual steps.
- [ ] `OPEN_QUESTIONS.md` is reviewed; any blocking items resolved or explicitly deferred to Phase 2 with a comment.

## What goes into the commit

A single PR titled `feat(phase-1): acceptance` containing:
- The integration test file.
- The acceptance report (filled in).
- Any documentation updates.
- Screenshots in `docs/screenshots/`.

This is the artifact that says "Phase 1 is real, and here's the evidence." Don't skip it — Phase 4's portfolio framing relies on having this trail.
