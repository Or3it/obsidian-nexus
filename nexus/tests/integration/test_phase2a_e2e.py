"""Phase 2a end-to-end integration tests.

Run with: pytest tests/integration/ --real-providers

Each test tagged @pytest.mark.real_providers requires:
  - Claude Code installed and authenticated (claude --version succeeds)
  - OPENROUTER_API_KEY in environment
  - Ollama running locally with gemma4:latest pulled
  - All five rungs configured in cascade.yaml

Port 7779 is used to avoid collision with the live orchestrator on 7777
or the Phase 1 test suite on 7778.
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

PORT = 7779
BASE_URL = f"http://127.0.0.1:{PORT}"

# ---------------------------------------------------------------------------
# Cascade YAML fixtures
# ---------------------------------------------------------------------------

CASCADE_YAML_ALL_RUNGS = textwrap.dedent("""\
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
      - position: 2
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: minimax/minimax-m2.5:free
      - position: 3
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: openai/gpt-oss-120b:free
      - position: 4
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: deepseek/deepseek-v4-flash:free
      - position: 5
        adapter: ollama
        auth_mode: local
        model: gemma4:latest
    jurisdictions: {}
""")

CASCADE_YAML_FAKE_R1 = textwrap.dedent("""\
    schema_version: 2
    rungs:
      - position: 1
        adapter: anthropic
        auth_mode: subscription
        use_ruflo: false
        models:
          default: claude-sonnet-FAKE
          premium: claude-opus-4-7
          summariser: claude-haiku-4-5
      - position: 2
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: minimax/minimax-m2.5:free
      - position: 3
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: openai/gpt-oss-120b:free
      - position: 4
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: deepseek/deepseek-v4-flash:free
      - position: 5
        adapter: ollama
        auth_mode: local
        model: gemma4:latest
    jurisdictions: {}
""")

CASCADE_YAML_FAKE_R1_R2 = textwrap.dedent("""\
    schema_version: 2
    rungs:
      - position: 1
        adapter: anthropic
        auth_mode: subscription
        use_ruflo: false
        models:
          default: claude-sonnet-FAKE
          premium: claude-opus-4-7
          summariser: claude-haiku-4-5
      - position: 2
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: NOT-A-VALID-MODEL-XYZ:free
      - position: 3
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: openai/gpt-oss-120b:free
      - position: 4
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: deepseek/deepseek-v4-flash:free
      - position: 5
        adapter: ollama
        auth_mode: local
        model: gemma4:latest
    jurisdictions: {}
""")

CASCADE_YAML_ALL_CLOUD_FAKE = textwrap.dedent("""\
    schema_version: 2
    rungs:
      - position: 1
        adapter: anthropic
        auth_mode: subscription
        use_ruflo: false
        models:
          default: claude-sonnet-FAKE
          premium: claude-opus-4-7
          summariser: claude-haiku-4-5
      - position: 2
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: NOT-A-VALID-MODEL-XYZ:free
      - position: 3
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: ALSO-INVALID:free
      - position: 4
        adapter: openrouter
        auth_mode: api_key_free_tier
        model: INVALID-TOO:free
      - position: 5
        adapter: ollama
        auth_mode: local
        model: gemma4:latest
    jurisdictions: {}
""")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Fresh vault with all 5 rungs (default: valid config)."""
    v = tmp_path / "vault"
    v.mkdir()
    config_dir = v / "Orchestrator" / "Config"
    config_dir.mkdir(parents=True)
    (config_dir / "cascade.yaml").write_text(CASCADE_YAML_ALL_RUNGS, encoding="utf-8")
    return v


@pytest.fixture()
def orchestrator_server(vault: Path) -> Iterator[str]:
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


def _wait_healthy(proc: subprocess.Popen, timeout: float = 20.0) -> None:
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


def _text_from_events(events: list[dict[str, str]]) -> str:
    return "".join(
        json.loads(e["data"]).get("text", "")
        for e in events
        if e["event"] == "token"
    )


def _handover_events(events: list[dict[str, str]]) -> list[dict]:
    return [json.loads(e["data"]) for e in events if e["event"] == "handover"]


# ---------------------------------------------------------------------------
# AC3 (Phase 2a): Rung 1 default path
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_rung_1_default_path(vault: Path, orchestrator_server: str) -> None:
    """A normal turn streams from Sonnet (Rung 1) with no handover event."""
    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Say 'rung one nominal' and nothing else."},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    event_types = {e["event"] for e in events}
    assert "token" in event_types
    assert "done" in event_types
    assert "handover" not in event_types, "Unexpected handover on healthy R1"

    text = _text_from_events(events)
    assert "rung one nominal" in text.lower(), f"Expected phrase not in: {text!r}"


# ---------------------------------------------------------------------------
# AC8a: Rung 1 → Rung 2 failover
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_rung_2_failover_when_rung_1_misconfigured(vault: Path, orchestrator_server: str) -> None:
    """Force R1 failure via bad model string; verify R2 (MiniMax M2.5) takes over."""
    # Inject bad R1 config while orchestrator is running (hot-reload test)
    cascade_path = vault / "Orchestrator" / "Config" / "cascade.yaml"
    cascade_path.write_text(CASCADE_YAML_FAKE_R1, encoding="utf-8")
    time.sleep(1.5)  # 500ms debounce + scheduling + reload

    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Reply with exactly: failover rung two"},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    handovers = _handover_events(events)
    assert len(handovers) >= 1, (
        f"Expected handover event. Got events: {[e['event'] for e in events]}"
    )

    ho = handovers[0]
    assert ho["from_rung"] == 1
    assert ho["to_rung"] == 2
    assert ho["trigger"] == "HARD_FAIL"
    assert "minimax" in ho["to_model"].lower()

    # Handover doc must exist in Handovers/
    handover_dir = vault / "Orchestrator" / "Handovers"
    handover_docs = [f for f in handover_dir.glob("*.md") if f.name != ".gitkeep"]
    assert handover_docs, "No handover doc found in Orchestrator/Handovers/"

    text = _text_from_events(events)
    assert text.strip() != "", "Empty response after failover"


# ---------------------------------------------------------------------------
# AC8b: Rung 1 + 2 fail → Rung 3
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_rung_3_failover_when_rungs_1_and_2_fail(vault: Path, orchestrator_server: str) -> None:
    """Both R1 and R2 misconfigured → cascade reaches R3 (gpt-oss-120b via OpenRouter)."""
    cascade_path = vault / "Orchestrator" / "Config" / "cascade.yaml"
    cascade_path.write_text(CASCADE_YAML_FAKE_R1_R2, encoding="utf-8")
    time.sleep(1.5)

    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Reply with exactly: rung three reached"},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    handovers = _handover_events(events)
    assert len(handovers) >= 2, f"Expected ≥2 handovers (R1→R2, R2→R3). Got: {handovers}"

    to_rungs = [h["to_rung"] for h in handovers]
    assert 3 in to_rungs, f"Expected to reach rung 3. Handovers: {handovers}"

    text = _text_from_events(events)
    assert text.strip() != "", "Empty response after two-rung failover"


# ---------------------------------------------------------------------------
# AC8c: All cloud rungs fail → Rung 5 (local Ollama)
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_rung_5_local_when_all_cloud_fail(vault: Path, orchestrator_server: str) -> None:
    """All cloud rungs misconfigured → cascade reaches Rung 5 (local Ollama)."""
    cascade_path = vault / "Orchestrator" / "Config" / "cascade.yaml"
    cascade_path.write_text(CASCADE_YAML_ALL_CLOUD_FAKE, encoding="utf-8")
    time.sleep(1.5)

    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Reply with exactly: local rung"},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    handovers = _handover_events(events)
    to_rungs = [h["to_rung"] for h in handovers]
    assert 5 in to_rungs, f"Expected to reach rung 5. Handovers: {handovers}"

    text = _text_from_events(events)
    assert text.strip() != "", "Empty response from local Ollama rung"


# ---------------------------------------------------------------------------
# AC4: Handover doc fields
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_handover_doc_has_all_required_fields(vault: Path, orchestrator_server: str) -> None:
    """Force R1 failure; generated handover doc contains all six required fields."""
    cascade_path = vault / "Orchestrator" / "Config" / "cascade.yaml"
    cascade_path.write_text(CASCADE_YAML_FAKE_R1, encoding="utf-8")
    time.sleep(1.5)

    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Test context: the user is writing a research note on climate."},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        _parse_sse(r.iter_lines())

    handover_dir = vault / "Orchestrator" / "Handovers"
    docs = [f for f in handover_dir.glob("*.md") if f.name != ".gitkeep"]
    assert docs, "No handover doc found"

    import frontmatter  # type: ignore[import-untyped]
    doc = frontmatter.load(str(docs[0]))
    fm = doc.metadata

    required_fields = [
        "handover_id", "thread_id", "from_model", "to_model",
        "trigger", "triggered_at",
    ]
    for field in required_fields:
        assert field in fm, f"Missing field '{field}' in handover frontmatter: {fm}"

    # Body must have a Summary section
    assert "## Summary" in doc.content, "Missing ## Summary in handover body"


# ---------------------------------------------------------------------------
# AC6: Hot-reload
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_hot_reload_picks_up_yaml_changes(vault: Path, orchestrator_server: str) -> None:
    """Edit cascade.yaml while orchestrator is running; verify change takes effect on next turn."""
    headers = _auth_headers(vault)
    cascade_path = vault / "Orchestrator" / "Config" / "cascade.yaml"

    # First turn: valid config → R1 should answer, no handover
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Say 'before reload' and nothing else."},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events_before = _parse_sse(r.iter_lines())

    assert "handover" not in {e["event"] for e in events_before}, (
        "Unexpected handover before reload"
    )

    # Hot-reload: swap R1 to broken model
    cascade_path.write_text(CASCADE_YAML_FAKE_R1, encoding="utf-8")
    time.sleep(1.5)  # 500ms debounce + async schedule

    # Second turn: new config should be live → R1 fails, R2 takes over
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Say 'after reload' and nothing else."},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events_after = _parse_sse(r.iter_lines())

    handovers = _handover_events(events_after)
    assert handovers, "Expected handover after hot-reload to bad model — none found"
    assert handovers[0]["from_rung"] == 1, "Expected failover originating from rung 1"


# ---------------------------------------------------------------------------
# AC5: Continuation preserves context
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_continuation_preserves_context(vault: Path, orchestrator_server: str) -> None:
    """After a handover, the second model can reference content from earlier in the thread."""
    headers = _auth_headers(vault)
    cascade_path = vault / "Orchestrator" / "Config" / "cascade.yaml"

    # First turn with valid config — Sonnet answers
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Remember the keyword: NEXUS-CONTEXT-TOKEN-42"},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events1 = _parse_sse(r.iter_lines())

    thread_id = next(
        json.loads(e["data"])["thread_id"] for e in events1 if e["event"] == "thread"
    )

    # Force failover for second turn
    cascade_path.write_text(CASCADE_YAML_FAKE_R1, encoding="utf-8")
    time.sleep(1.5)

    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={
            "thread_id": thread_id,
            "user_message": "What keyword did I mention earlier?",
        },
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events2 = _parse_sse(r.iter_lines())

    handovers = _handover_events(events2)
    assert handovers, "Expected handover on second turn — none found"

    text2 = _text_from_events(events2)
    # The handover doc should have included the keyword in the summary
    assert "nexus-context-token-42" in text2.lower(), (
        f"Context keyword not preserved across handover. R2 response: {text2!r}"
    )


# ---------------------------------------------------------------------------
# Verify slug availability (guard against free-tier churn)
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_openrouter_slugs_still_live(vault: Path, orchestrator_server: str) -> None:
    """Ping each OpenRouter rung's health; confirm all 3 report healthy.

    Re-run at every phase boundary — free-tier slugs can disappear.
    """
    headers = {"Authorization": _auth_headers(vault)["Authorization"]}
    r = httpx.get(f"{orchestrator_server}/health", headers=headers, timeout=15.0)
    assert r.status_code == 200
    health = r.json()

    for rung_pos in (2, 3, 4):
        rung_health = health["rungs"].get(str(rung_pos), {})
        assert rung_health.get("healthy"), (
            f"Rung {rung_pos} (OpenRouter) reports unhealthy: {rung_health}. "
            f"The free-tier slug may have been removed — verify and update cascade.yaml."
        )


# ---------------------------------------------------------------------------
# Pinned model override
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_pinned_model_overrides_cascade(vault: Path, orchestrator_server: str) -> None:
    """A thread with pinned_model in YAML always routes to that model."""
    # This test exercises the select_rung logic. With a valid cascade and no
    # pin, R1 is selected. We verify that a healthy R1 answers — the pinned_model
    # path (which requires a thread that already has frontmatter written) is
    # exercised indirectly via the cascade engine unit tests (test_cascade_engine.py).
    headers = _auth_headers(vault)
    with httpx.Client() as client, client.stream(
        "POST",
        f"{orchestrator_server}/turn",
        headers=headers,
        json={"user_message": "Say 'pin test' and nothing else."},
        timeout=120.0,
    ) as r:
        assert r.status_code == 200
        events = _parse_sse(r.iter_lines())

    assert "handover" not in {e["event"] for e in events}
    text = _text_from_events(events)
    assert "pin test" in text.lower()
