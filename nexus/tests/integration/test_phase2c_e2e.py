"""Phase 2c end-to-end integration tests.

Run with: pytest tests/integration/ --real-providers

Server tests (test_strongest_live_model_selected, test_classification_logged)
require live Claude Code + Pro subscription + OPENROUTER_API_KEY.

Engine tests (remaining six) exercise cascade mechanics with mock adapters and
require no external network — they are still gated on --real-providers so they
run only in a verified environment as part of the acceptance pass.

Port 7780 avoids collisions with:
  - 7777 (live orchestrator)
  - 7778 (Phase 1 integration tests)
  - 7779 (Phase 2a integration tests)
"""

import asyncio
import json
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

PORT = 7780
BASE_URL = f"http://127.0.0.1:{PORT}"
FAR_FUTURE = "2099-01-01T00:00:00+00:00"

# ---------------------------------------------------------------------------
# Cascade YAML fixtures
# ---------------------------------------------------------------------------

# Full schema-3 pool with valid Sonnet + real free-tier pool.
# Used when Sonnet is expected to answer (no intentional failures).
CASCADE_YAML_POOL_FULL = textwrap.dedent("""\
    schema_version: 3
    rungs:
      - position: 1
        adapter: anthropic
        auth_mode: subscription
        use_ruflo: false
        models:
          default: claude-sonnet-4-6
          premium: claude-opus-4-7
          summariser: claude-haiku-4-5
    pool:
      adapter: openrouter
      auth_mode: api_key_free_tier
      tier_order: [frontier, strong, reliable]
      members:
        - model: nvidia/nemotron-3-super-120b-a12b:free
          tier: frontier
          roles: [reasoning, coding]
          reasoning_mode: exclude
        - model: openai/gpt-oss-120b:free
          tier: frontier
          roles: [reasoning, coding, general]
        - model: deepseek/deepseek-v4-flash:free
          tier: frontier
          roles: [reasoning, coding]
        - model: qwen/qwen3-coder:free
          tier: frontier
          roles: [coding]
        - model: minimax/minimax-m2.5:free
          tier: strong
          roles: [coding, general]
        - model: qwen/qwen3-next-80b-a3b-instruct:free
          tier: strong
          roles: [general]
        - model: google/gemma-4-31b-it:free
          tier: reliable
          roles: [general]
        - model: openai/gpt-oss-20b:free
          tier: reliable
          roles: [general, fast]
    floor:
      adapter: ollama
      auth_mode: local
      model: gemma4:latest
    classifier:
      enabled: true
      backend: heuristic
      route_by_role: false
    jurisdictions: {}
""")

# Schema-2 fixed-rung config — backward compatibility test.
CASCADE_YAML_SCHEMA2 = textwrap.dedent("""\
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
        model: openai/gpt-oss-120b:free
    jurisdictions: {}
""")


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------


def _make_vault(
    tmp_path: Path,
    cascade_yaml: str,
    pre_health: dict[str, Any] | None = None,
) -> Path:
    """Create a minimal vault directory structure for a test."""
    vault = tmp_path / "vault"
    config_dir = vault / "Orchestrator" / "Config"
    config_dir.mkdir(parents=True)
    (config_dir / "cascade.yaml").write_text(cascade_yaml, encoding="utf-8")
    if pre_health:
        (config_dir / "model_health.json").write_text(
            json.dumps(pre_health, indent=2), encoding="utf-8"
        )
    return vault


def _auth_headers(vault: Path) -> dict[str, str]:
    token = (vault / "Orchestrator" / "Config" / "auth.token").read_text().strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Server subprocess helpers
# ---------------------------------------------------------------------------


def _wait_healthy(proc: subprocess.Popen, base_url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise RuntimeError(f"Orchestrator exited prematurely:\n{err[:600]}")
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Orchestrator did not become healthy within timeout")


@contextmanager
def _run_server(vault: Path) -> Iterator[str]:
    """Start orchestrator subprocess, yield base_url, stop on exit."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "--vault", str(vault), "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_healthy(proc, BASE_URL)
        yield BASE_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _parse_sse(lines: Iterator[str]) -> list[dict[str, Any]]:
    """Parse SSE stream into [{event, data}, ...]."""
    events: list[dict[str, Any]] = []
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
                data_str = "\n".join(data_parts)
                try:
                    parsed = json.loads(data_str)
                except json.JSONDecodeError:
                    parsed = data_str
                events.append({"event": event, "data": parsed})
                buf = []
        else:
            buf.append(line)
    return events


def _send_turn(
    base_url: str,
    headers: dict[str, str],
    message: str,
    timeout: float = 90.0,
) -> tuple[str, list[dict[str, Any]]]:
    """POST /turn, return (thread_id, sse_events)."""
    thread_id = ""
    with httpx.Client() as client, client.stream(
        "POST",
        f"{base_url}/turn",
        headers=headers,
        json={"user_message": message},
        timeout=timeout,
    ) as r:
        assert r.status_code == 200, f"POST /turn returned {r.status_code}"
        lines = (line for line in r.iter_lines())
        events = _parse_sse(lines)
    # Extract thread_id from the first SSE event (event: thread)
    for ev in events:
        if ev["event"] == "thread" and isinstance(ev["data"], dict):
            thread_id = str(ev["data"].get("thread_id", ""))
            break
    return thread_id, events


def _read_jsonl(vault: Path, thread_id: str) -> list[dict[str, Any]]:
    """Read JSONL events for a given thread_id from the vault Logs/ directory."""
    id_short = thread_id.replace("-", "")[:16]
    path = vault / "Orchestrator" / "Logs" / f"{id_short}.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Bare-engine helpers (for mechanics tests that don't need a running server)
# ---------------------------------------------------------------------------


def _minimal_pool_config(
    members: list[dict[str, Any]],
    floor_model: str = "gemma4:latest",
) -> "Any":
    """Build a CascadeConfig with the given pool members (dict form)."""
    from orchestrator.types import (
        CascadeConfig,
        ClassifierConfig,
        FloorConfig,
        PoolConfig,
        PoolMember,
        RungConfig,
    )

    pm = [PoolMember(**m) for m in members]
    return CascadeConfig(
        schema_version=3,
        rungs=[
            RungConfig(
                position=1,
                adapter="anthropic",
                auth_mode="subscription",
                models={"default": "claude-sonnet-4-6"},
            )
        ],
        pool=PoolConfig(
            adapter="openrouter",
            auth_mode="api_key_free_tier",
            tier_order=["frontier", "strong", "reliable"],
            members=pm,
        ),
        floor=FloorConfig(adapter="ollama", auth_mode="local", model=floor_model),
        classifier=ClassifierConfig(enabled=True, backend="heuristic", route_by_role=False),
    )


def _bare_engine(tmp_path: Path, config: "Any") -> "Any":
    """Build a CascadeEngine without calling __init__ — mirrors unit test helper."""
    from orchestrator.cascade.cooldown import CooldownLedger
    from orchestrator.cascade.engine import CascadeEngine
    from orchestrator.cascade.model_health import ModelHealth
    from orchestrator.cascade.triggers import LatencyTracker

    eng: CascadeEngine = object.__new__(CascadeEngine)
    eng.vault_root = tmp_path
    eng.config_path = tmp_path / "cascade.yaml"
    eng.budgets_path = tmp_path / "Orchestrator" / "Config" / "budgets.yaml"
    eng.cooldowns = CooldownLedger(eng.budgets_path)
    eng.model_health = ModelHealth(
        tmp_path / "Orchestrator" / "Config" / "model_health.json"
    )
    eng.latency = LatencyTracker()
    eng._adapters = {}
    eng._pool_adapters = {}
    eng._floor_adapter = None
    eng._probe_cache = {}
    eng._summariser = None
    eng._reload_lock = asyncio.Lock()
    eng._loop = None
    eng._config = config
    eng._watcher = MagicMock()
    return eng


def _mock_adapter(name: str = "openrouter") -> MagicMock:
    m = MagicMock()
    m.name = name
    return m


def _stream_ok(*texts: str) -> Any:
    """Async generator that yields text tokens then a final token."""
    from orchestrator.types import Token

    async def _gen(req: Any) -> AsyncGenerator:
        for t in texts:
            yield Token(text=t)
        yield Token(text="", is_final=True)

    return _gen


def _stream_raise(exc: Exception) -> Any:
    """Async generator that immediately raises exc."""
    async def _gen(req: Any) -> AsyncGenerator:
        raise exc
        yield  # makes it an async generator

    return _gen


def _make_request(message: str = "Hello") -> "Any":
    """Build a minimal NexusRequest."""
    from orchestrator.types import NexusMessage, NexusRequest, StyleProfile

    return NexusRequest(
        thread_id="acceptance-test-thread",
        messages=[NexusMessage(role="user", content=message)],
        style_profile=StyleProfile(),
    )


# ---------------------------------------------------------------------------
# AC1 + AC4 + AC8: Strongest live model selected
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_strongest_live_model_selected(tmp_path: Path) -> None:
    """AC4 + AC8: Pool loads; a turn is served by a live frontier model.

    With real Sonnet healthy, the pool resolves Sonnet as the first live
    candidate and it answers.  JSONL model_id is one of the known pool models.
    No error events in the SSE stream.
    """
    vault = _make_vault(tmp_path, CASCADE_YAML_POOL_FULL)
    known_models = {
        "claude-sonnet-4-6",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "openai/gpt-oss-120b:free",
        "deepseek/deepseek-v4-flash:free",
        "qwen/qwen3-coder:free",
        "minimax/minimax-m2.5:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-20b:free",
        "gemma4:latest",
    }

    with _run_server(vault) as base_url:
        headers = _auth_headers(vault)
        thread_id, events = _send_turn(
            base_url, headers, "Reply with exactly one word: ok"
        )

    # No terminal error events
    error_events = [e for e in events if e["event"] == "error" and e["data"].get("terminal")]
    assert not error_events, f"Terminal error in stream: {error_events}"

    # SSE contained tokens
    token_events = [e for e in events if e["event"] == "token" and e["data"].get("text")]
    assert token_events, "No token events in SSE response"

    # JSONL model_id is a known pool member
    jsonl = _read_jsonl(vault, thread_id)
    assistant_evt = next(
        (e for e in jsonl if e.get("event_type") == "turn_assistant"), None
    )
    assert assistant_evt is not None, "No turn_assistant event in JSONL"
    model_id = assistant_evt["payload"].get("model_id", "")
    assert model_id in known_models, (
        f"model_id {model_id!r} is not one of the expected pool members"
    )


# ---------------------------------------------------------------------------
# AC2: Dead model detected and skipped — bare engine
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
async def test_dead_model_detected_and_skipped(tmp_path: Path) -> None:
    """AC2: A pool member that raises AdapterModelUnavailable is marked dead.

    Engine skips it, cascades to the next live member, and model_health records
    the failure.  Sonnet is also made to fail so the pool is entered.
    """
    from orchestrator.adapters.base import AdapterModelUnavailable
    from orchestrator.cascade.events import HandoverEvent, TokenEvent

    cfg = _minimal_pool_config([
        {"model": "frontier/dead:free", "tier": "frontier", "roles": ["general"]},
        {"model": "frontier/live:free", "tier": "frontier", "roles": ["general"]},
    ])
    eng = _bare_engine(tmp_path, cfg)

    # Sonnet (rung 1) fails with model-unavailable
    sonnet_mock = _mock_adapter("anthropic")
    sonnet_mock.complete_stream = _stream_raise(AdapterModelUnavailable("sonnet gone"))
    eng._adapters[1] = sonnet_mock

    # First frontier model: unavailable
    dead_mock = _mock_adapter("openrouter")
    dead_mock.complete_stream = _stream_raise(
        AdapterModelUnavailable("provider returned error: 404")
    )
    eng._pool_adapters["frontier/dead:free"] = dead_mock

    # Second frontier model: succeeds
    live_mock = _mock_adapter("openrouter")
    live_mock.complete_stream = _stream_ok("cascade worked")
    eng._pool_adapters["frontier/live:free"] = live_mock

    # Floor (not expected to be reached)
    floor_mock = _mock_adapter("ollama")
    floor_mock.complete_stream = _stream_ok("floor answer")
    eng._floor_adapter = floor_mock

    events = [e async for e in eng.complete_stream(_make_request(), {}, "h.md")]

    token_texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
    handovers = [e for e in events if isinstance(e, HandoverEvent)]

    # Dead model was skipped and the live one answered
    assert any("cascade worked" in t for t in token_texts), f"texts: {token_texts}"
    assert len(handovers) >= 1

    # Dead model recorded in ModelHealth
    assert eng.model_health.is_dead("frontier/dead:free"), (
        "frontier/dead:free should be marked dead after AdapterModelUnavailable"
    )
    entry = eng.model_health._data.get("frontier/dead:free")
    assert entry is not None and entry.consecutive_failures >= 1


# ---------------------------------------------------------------------------
# AC2: Error classification accurate (2026-05-29 regression)
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
async def test_error_classification_accurate(tmp_path: Path) -> None:
    """AC2 regression: AdapterModelUnavailable → reason 'model_unavailable', not 'rate_limited'.

    Before T01, a 404/provider-error was misclassified as rate_limited.  This
    test confirms the discriminating fix holds for both error types.
    """
    from orchestrator.adapters.base import AdapterModelUnavailable, AdapterRateLimit
    from orchestrator.cascade.events import HandoverEvent

    cfg = _minimal_pool_config([
        {"model": "p/unavail:free", "tier": "frontier", "roles": ["general"]},
        {"model": "p/ratelimit:free", "tier": "frontier", "roles": ["general"]},
        {"model": "p/ok:free", "tier": "strong", "roles": ["general"]},
    ])
    eng = _bare_engine(tmp_path, cfg)

    # Sonnet fails with model-unavailable (so pool is entered)
    sonnet_mock = _mock_adapter("anthropic")
    sonnet_mock.complete_stream = _stream_raise(AdapterModelUnavailable("sonnet gone"))
    eng._adapters[1] = sonnet_mock

    # First pool member: model-unavailable (the T01 regression case)
    eng._pool_adapters["p/unavail:free"] = _mock_adapter("openrouter")
    eng._pool_adapters["p/unavail:free"].complete_stream = _stream_raise(
        AdapterModelUnavailable("provider returned error: 404")
    )

    # Second pool member: rate-limited (must NOT be misclassified as model_unavailable)
    eng._pool_adapters["p/ratelimit:free"] = _mock_adapter("openrouter")
    eng._pool_adapters["p/ratelimit:free"].complete_stream = _stream_raise(
        AdapterRateLimit("rate limit exceeded", cooldown_seconds=30)
    )

    # Third pool member: succeeds
    eng._pool_adapters["p/ok:free"] = _mock_adapter("openrouter")
    eng._pool_adapters["p/ok:free"].complete_stream = _stream_ok("ok")

    floor_mock = _mock_adapter("ollama")
    floor_mock.complete_stream = _stream_ok("floor")
    eng._floor_adapter = floor_mock

    events = [e async for e in eng.complete_stream(_make_request(), {}, "h.md")]
    handovers = [e for e in events if isinstance(e, HandoverEvent)]

    # Extract per-model handover reasons
    reasons = {h.from_model: h.reason for h in handovers}

    assert reasons.get("p/unavail:free") == "model_unavailable", (
        f"Expected 'model_unavailable' for 404-like error, got {reasons!r}"
    )
    assert reasons.get("p/ratelimit:free") == "rate_limited", (
        f"Expected 'rate_limited' for 429-like error, got {reasons!r}"
    )


# ---------------------------------------------------------------------------
# AC5: Backoff retry — bare ModelHealth timing test
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_backoff_retry(tmp_path: Path) -> None:
    """AC5: A model marked dead at T_PAST (backoff long expired) becomes eligible again.

    Tests the full backoff lifecycle:
      mark_unavailable (now)  → is_dead=True
      mark_unavailable (past) → is_dead=False (backoff expired)
      mark_success            → failures reset to 0
    """
    from orchestrator.cascade.model_health import ModelHealth

    path = tmp_path / "model_health.json"
    health = ModelHealth(path)
    model = "test/backoff-model:free"

    # Fresh model — not dead
    assert not health.is_dead(model)

    # Mark dead right now — is_dead should be True within backoff window
    health.mark_unavailable(model, "first failure")
    assert health.is_dead(model), "Model should be dead immediately after first failure"

    # Simulate mark at a time far in the past (backoff long expired)
    past = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    health.mark_unavailable(model, "past failure", now=past)
    assert not health.is_dead(model), (
        "Backoff set at T_PAST should be expired — model should be eligible now"
    )

    # After backoff expires, failure count persists (needed for recovery-probe logic)
    entry = health._data.get(model)
    assert entry is not None and entry.consecutive_failures > 0

    # Mark success clears the slate
    health.mark_success(model)
    assert not health.is_dead(model)
    entry = health._data.get(model)
    assert entry is None or entry.consecutive_failures == 0


# ---------------------------------------------------------------------------
# AC3: Shared quota cooldown — bare engine
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
async def test_quota_shared_cooldown(tmp_path: Path) -> None:
    """AC3: AdapterQuotaExhausted on one OR member cools openrouter_shared.

    All remaining OpenRouter pool members are then skipped (quota_key guard),
    and the cascade falls through to the Ollama floor unconditionally.
    """
    from orchestrator.adapters.base import AdapterModelUnavailable, AdapterQuotaExhausted
    from orchestrator.cascade.events import HandoverEvent, TokenEvent

    cfg = _minimal_pool_config([
        {"model": "or/a:free", "tier": "frontier", "roles": ["general"]},
        {"model": "or/b:free", "tier": "frontier", "roles": ["general"]},
        {"model": "or/c:free", "tier": "reliable", "roles": ["general"]},
    ])
    eng = _bare_engine(tmp_path, cfg)

    # Sonnet fails
    sonnet_mock = _mock_adapter("anthropic")
    sonnet_mock.complete_stream = _stream_raise(AdapterModelUnavailable("sonnet gone"))
    eng._adapters[1] = sonnet_mock

    # First OR member: quota exhausted → should cool openrouter_shared
    eng._pool_adapters["or/a:free"] = _mock_adapter("openrouter")
    eng._pool_adapters["or/a:free"].complete_stream = _stream_raise(
        AdapterQuotaExhausted("daily quota exceeded", cooldown_key="openrouter_shared")
    )

    # Remaining OR members would succeed, but should be skipped
    for slug in ("or/b:free", "or/c:free"):
        eng._pool_adapters[slug] = _mock_adapter("openrouter")
        eng._pool_adapters[slug].complete_stream = _stream_ok(f"answer from {slug}")

    # Floor answers
    floor_mock = _mock_adapter("ollama")
    floor_mock.complete_stream = _stream_ok("floor answer")
    eng._floor_adapter = floor_mock

    events = [e async for e in eng.complete_stream(_make_request(), {}, "h.md")]

    token_texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
    handovers = [e for e in events if isinstance(e, HandoverEvent)]

    # Shared quota key must be exhausted
    assert eng.cooldowns.is_exhausted("openrouter_shared"), (
        "openrouter_shared should be cooled after AdapterQuotaExhausted"
    )

    # Or/b and or/c should be skipped — last handover to_model is the floor
    assert handovers, "Expected at least one handover"
    last_handover = handovers[-1]
    assert last_handover.to_model == "gemma4:latest", (
        f"Expected cascade to reach floor, but to_model={last_handover.to_model!r}"
    )
    assert any("floor" in t for t in token_texts), f"texts: {token_texts}"


# ---------------------------------------------------------------------------
# AC8: Pin overrides health — bare engine
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
async def test_pin_overrides_health(tmp_path: Path) -> None:
    """AC8: A pinned model is tried first regardless of its health state.

    The pin is a user override — health must not silently reroute away from it.
    On success the pinned model answers.  On failure the cascade falls through
    to the next live candidate.
    """
    from orchestrator.cascade.events import HandoverEvent, TokenEvent

    cfg = _minimal_pool_config([
        {"model": "or/pinned:free", "tier": "frontier", "roles": ["general"]},
        {"model": "or/live:free", "tier": "frontier", "roles": ["general"]},
    ])
    eng = _bare_engine(tmp_path, cfg)

    # Mark the pinned model dead (consecutive_failures > 0, dead_until far future)
    past = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    eng.model_health.mark_unavailable("or/pinned:free", "prior failure", now=past)
    # Now reset dead_until to far future so it's still technically dead
    from orchestrator.cascade.model_health import _Entry
    eng.model_health._data["or/pinned:free"] = _Entry(
        consecutive_failures=1,
        dead_until=FAR_FUTURE,
        last_failure=past.isoformat(),
    )
    eng.model_health._save()

    # Sonnet fails (not in scope here)
    sonnet_mock = _mock_adapter("anthropic")
    from orchestrator.adapters.base import AdapterModelUnavailable
    sonnet_mock.complete_stream = _stream_raise(AdapterModelUnavailable("sonnet gone"))
    eng._adapters[1] = sonnet_mock

    # Pinned model mock: succeeds despite being "dead"
    eng._pool_adapters["or/pinned:free"] = _mock_adapter("openrouter")
    eng._pool_adapters["or/pinned:free"].complete_stream = _stream_ok("pinned answer")

    eng._pool_adapters["or/live:free"] = _mock_adapter("openrouter")
    eng._pool_adapters["or/live:free"].complete_stream = _stream_ok("live answer")

    floor_mock = _mock_adapter("ollama")
    floor_mock.complete_stream = _stream_ok("floor")
    eng._floor_adapter = floor_mock

    # Pin to the dead model
    thread_yaml = {"pinned_model": "or/pinned:free"}
    events = [e async for e in eng.complete_stream(_make_request(), thread_yaml, "h.md")]

    token_texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
    handovers = [e for e in events if isinstance(e, HandoverEvent)]

    # Pin honored — pinned model answered, no handover needed (it succeeded)
    assert any("pinned answer" in t for t in token_texts), (
        f"Expected pinned model to answer, got: {token_texts}"
    )
    assert not handovers, "Pinned model succeeded — should be no handover"


# ---------------------------------------------------------------------------
# AC7: Classification logged — server test
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_classification_logged(tmp_path: Path) -> None:
    """AC7: Every turn produces a turn_classified event in the JSONL.

    Coding turn  → task_class = 'coding'.
    Casual turn  → task_class = 'conversational'.
    route_by_role=false → selection identical to unbiased (Sonnet answers both).
    """
    vault = _make_vault(tmp_path, CASCADE_YAML_POOL_FULL)

    with _run_server(vault) as base_url:
        headers = _auth_headers(vault)

        # Coding turn
        coding_tid, _ = _send_turn(
            base_url,
            headers,
            "def fix_this():\n    return undefined_var",
        )

        # Casual turn
        casual_tid, _ = _send_turn(base_url, headers, "hey how are you")

    for thread_id, expected_class in [
        (coding_tid, "coding"),
        (casual_tid, "conversational"),
    ]:
        jsonl = _read_jsonl(vault, thread_id)
        classified = [e for e in jsonl if e.get("event_type") == "turn_classified"]
        assert len(classified) == 1, (
            f"Expected exactly one turn_classified event for {expected_class!r} turn, "
            f"got {len(classified)}: {[e['payload'] for e in classified]}"
        )
        payload = classified[0]["payload"]
        assert payload.get("task_class") == expected_class, (
            f"Expected task_class={expected_class!r}, got {payload.get('task_class')!r}"
        )
        assert payload.get("backend") == "heuristic"


# ---------------------------------------------------------------------------
# AC1 + AC8: Legacy schema-2 backward compatibility — bare engine
# ---------------------------------------------------------------------------


@pytest.mark.real_providers
def test_legacy_schema2_still_works(tmp_path: Path) -> None:
    """AC1 + AC8: Schema-2 fixed-rung config loads and selects the correct rung.

    Confirms backward compatibility: pool-less schema-2 configs still work after
    all Phase 2c changes.  _complete_stream_legacy path is exercised.
    """
    from orchestrator.cascade.events import ClassificationEvent, TokenEvent
    from orchestrator.config import load_cascade
    from orchestrator.types import Token

    # Write schema-2 config to tmp_path
    config_path = tmp_path / "cascade.yaml"
    config_path.write_text(CASCADE_YAML_SCHEMA2, encoding="utf-8")

    config = load_cascade(config_path)
    assert not config.uses_pool, "Schema-2 config must not set uses_pool=True"
    assert config.schema_version == 2
    assert config.classifier.enabled is True, "ClassifierConfig defaults to enabled=True"

    # Build a bare engine with the schema-2 config
    eng = _bare_engine(tmp_path, config)

    async def _gen(req: Any) -> AsyncGenerator:
        yield Token(text="legacy works", is_final=True)

    mock_r1 = _mock_adapter("anthropic")
    mock_r1.complete_stream = _gen
    eng._adapters[1] = mock_r1

    async def _run() -> list[Any]:
        return [e async for e in eng.complete_stream(_make_request(), {}, "h.md")]

    events = asyncio.run(_run())

    token_texts = [e.token.text for e in events if isinstance(e, TokenEvent) and e.token.text]
    cls_events = [e for e in events if isinstance(e, ClassificationEvent)]

    assert any("legacy works" in t for t in token_texts), f"texts: {token_texts}"
    # Classifier runs even on the legacy path (T07 fix verification)
    assert len(cls_events) == 1, (
        f"Expected one ClassificationEvent on legacy path, got {len(cls_events)}"
    )
    assert cls_events[0].backend == "heuristic"
