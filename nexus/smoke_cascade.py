"""P2a-T08 manual smoke test: forced Rung-1 failure → handover → Rung-2 success.

Usage (from nexus/nexus/):
    .venv/bin/python smoke_cascade.py

Precondition: ~/nexus-test-vault/Orchestrator/Config/cascade.yaml must contain
two rungs — rung 1 with adapter=anthropic and a fake/invalid model name,
rung 2 with adapter=ollama and model=gemma4:latest.
"""

import asyncio
import sys
from pathlib import Path

# Add nexus/nexus to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from orchestrator.cascade.engine import CascadeEngine
from orchestrator.cascade.events import ErrorEvent, HandoverEvent, TokenEvent
from orchestrator.types import NexusMessage, NexusRequest, StyleProfile

VAULT_ROOT = Path.home() / "nexus-test-vault"
CONFIG_PATH = VAULT_ROOT / "Orchestrator" / "Config" / "cascade.yaml"
THREAD_ID = "smoke-t08-00000001"


async def main() -> int:
    print(f"Vault root : {VAULT_ROOT}")
    print(f"Config     : {CONFIG_PATH}")
    print()

    engine = CascadeEngine(vault_root=VAULT_ROOT, config_path=CONFIG_PATH)

    request = NexusRequest(
        thread_id=THREAD_ID,
        messages=[
            NexusMessage(role="user", content="What is 2+2? Answer in one sentence."),
        ],
        style_profile=StyleProfile(),
    )

    events = []
    print("Streaming events:")
    async for event in engine.complete_stream(request, {}, "smoke_t08_history.md"):
        events.append(event)
        if isinstance(event, TokenEvent):
            print(f"  TOKEN      : {event.token.text!r} (final={event.token.is_final})")
        elif isinstance(event, HandoverEvent):
            print(
                f"  HANDOVER   : rung {event.from_rung} ({event.from_model})"
                f" → rung {event.to_rung} ({event.to_model})"
                f" | trigger={event.trigger}"
            )
            print(f"             : doc={event.handover_path}")
        elif isinstance(event, ErrorEvent):
            print(f"  ERROR      : {event.message!r} (terminal={event.terminal})")

    engine.shutdown()
    print()

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------
    handover_events = [e for e in events if isinstance(e, HandoverEvent)]
    token_events = [e for e in events if isinstance(e, TokenEvent) and e.token.text]
    error_events = [e for e in events if isinstance(e, ErrorEvent) and e.terminal]

    ok = True

    # (a) Handover doc on disk
    if handover_events:
        doc_path = handover_events[0].handover_path
        if doc_path.exists():
            print(f"[PASS] Handover doc created: {doc_path.name}")
        else:
            print(f"[FAIL] HandoverEvent emitted but file not found: {doc_path}")
            ok = False
    else:
        print("[FAIL] No HandoverEvent emitted — failover did not trigger")
        ok = False

    # (b) Turn ultimately succeeded on rung 2
    if handover_events and token_events:
        rung2_token = next(
            (e for e in events if isinstance(e, TokenEvent) and e.token.text and
             events.index(e) > events.index(handover_events[-1])),
            None,
        )
        if rung2_token:
            print(f"[PASS] Turn succeeded on rung {handover_events[-1].to_rung}")
        else:
            print("[FAIL] No tokens received after handover")
            ok = False
    elif error_events:
        print("[FAIL] Terminal error — turn did not succeed on any rung")
        ok = False

    print()
    print("SMOKE TEST PASSED" if ok else "SMOKE TEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
