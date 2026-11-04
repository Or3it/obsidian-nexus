# P1-T06 — Orchestrator Bridge (HTTP/IPC + Wiring)

## Goal

Wire the event log, projection, and Anthropic adapter into a coherent service the Obsidian plugin can talk to. Phase 1 ships a localhost HTTP server with two endpoints: send a turn, stream the response.

## Read first

- `shared/data-architecture.md`
- `shared/adapter-protocol.md`
- All prior Phase 1 tickets (T02 through T05) — this is where they compose.

## Files to create or replace

```
orchestrator/orchestrator.py         ← replaces the stub
orchestrator/server.py               ← new — FastAPI app
orchestrator/__main__.py             ← entrypoint: `python -m orchestrator`
orchestrator/prompts.py              ← new — pure functions for prompt construction
tests/test_orchestrator.py
tests/test_prompts.py
tests/test_server.py
```

## Additional dependency

Add to `pyproject.toml`:

```toml
"fastapi>=0.110",
"uvicorn[standard]>=0.27",
"watchdog>=4.0",            # for hot-reload of config (interface only in Phase 1)
```

## Architecture

```
plugin (TS)  ──HTTP──>  server.py (FastAPI)  ──>  orchestrator.py (logic)
                                                       ↓
                                          adapter (Anthropic)    vault (EventLog + projection)
                                                       ↑
                                              prompts.py (pure functions)
```

Localhost only. Default port `7777`. Auth: a per-install secret token in `Config/auth.token` (random hex, generated on first run), required as a bearer header on every plugin → server request. The plugin reads the token from the same file.

## `orchestrator/prompts.py` — pure functions for prompt construction

This module is intentionally split out from `orchestrator.py`. Phase 1's logic is trivial — just stack history, system message from style profile, append user message. But Phase 3 will add retrieval-injected context and tier-budget packing here. Keeping these as **pure functions with stable signatures** means Phase 3 modifies these functions' bodies without changing call sites.

```python
"""Prompt construction. Pure functions, no I/O.

Phase 1: trivial implementations.
Phase 2: handover-document insertion at model-boundary positions.
Phase 3: tier-budget context packing with retrieved archive + vault context.
"""
from .types import NexusMessage, StyleProfile


def build_system_prompt(style: StyleProfile) -> str:
    """
    Construct the system prompt for the active model.

    Phase 1: minimal — style profile only.
    Phase 3: retrieved archive context + vault notes get injected here.

    Keep this signature stable so Phase 3 doesn't require a refactor.
    Future signature may grow optional kwargs for handover, archive_items,
    vault_items; today it just renders the style profile.
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


def build_message_history(
    history: list[NexusMessage],
    user_msg: str,
) -> list[NexusMessage]:
    """
    Stack prior turns + the current user message into a message list.

    Phase 1: simple linear stack.
    Phase 2: handover documents will be inserted at model-boundary positions.
    Phase 3: tier-budget packing decides what to keep verbatim, what to summarise,
             and what to drop — but the *output type* (list[NexusMessage]) stays
             the same. Callers don't need to change.
    """
    messages = list(history)
    messages.append(NexusMessage(role="user", content=user_msg))
    return messages


def compose_request_messages(
    history: list[NexusMessage],
    user_msg: str,
    style: StyleProfile,
) -> list[NexusMessage]:
    """Compose system prompt + message history into the adapter's input."""
    system_prompt = build_system_prompt(style)
    messages: list[NexusMessage] = []
    if system_prompt:
        messages.append(NexusMessage(role="system", content=system_prompt))
    messages.extend(build_message_history(history, user_msg))
    return messages
```

## `orchestrator/orchestrator.py`

```python
from pathlib import Path
from datetime import datetime, timezone
from typing import AsyncIterator
import uuid_utils as uuid
from .types import NexusRequest, NexusMessage, StyleProfile, Token
from .adapters.base import Adapter, AdapterError
from .adapters.anthropic import AnthropicAdapter
from .vault.event_log import EventLog
from .vault.projection import project_thread
from .config import load_cascade, load_tokenizer_ratios
from .prompts import compose_request_messages


class Orchestrator:
    """
    Phase 1: single-adapter (Anthropic) orchestrator.
    Owns vault writes. Adapter never touches the vault.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.cascade = load_cascade(vault_root / "Orchestrator" / "Config" / "cascade.yaml")
        self.ratios = load_tokenizer_ratios(vault_root / "Orchestrator" / "Config" / "tokenizer_ratios.yaml")
        rung1 = self.cascade.rungs[0]
        if rung1.adapter != "anthropic":
            raise ValueError("Phase 1 requires rung 1 to be the Anthropic adapter.")
        default_model = (rung1.models or {}).get("default", "claude-sonnet-4-6")
        self.adapter: Adapter = AnthropicAdapter(
            model=default_model,
            use_ruflo=rung1.use_ruflo,
            ruflo_version=rung1.ruflo_version,
            tokenizer_ratio=self.ratios.ratios.get(default_model, self.ratios.default),
        )
        self._ensure_vault_layout()

    def _ensure_vault_layout(self) -> None:
        """Create Orchestrator/ directories and sentinel file if missing."""
        ...

    def _log_path(self, thread_id: str) -> Path:
        id_short = thread_id.replace("-", "")[:8]
        return self.vault_root / "Orchestrator" / "Logs" / f"{id_short}.jsonl"

    def new_thread_id(self) -> str:
        return str(uuid.uuid7())

    async def send_turn_stream(
        self,
        thread_id: str,
        user_message: str,
        style_profile: StyleProfile | None = None,
    ) -> AsyncIterator[Token]:
        """
        Append user turn, ask adapter, stream tokens to caller while
        buffering them; on completion, append assistant turn; project.
        """
        log = EventLog(self._log_path(thread_id))
        history = self._load_history(log)
        turn_number = self._next_turn_number(history)
        log.append("turn_user", {"content": user_message, "turn_number": turn_number})

        style = style_profile or StyleProfile()
        messages = compose_request_messages(history, user_message, style)
        request = NexusRequest(
            thread_id=thread_id,
            messages=messages,
            style_profile=style,
        )

        chunks: list[str] = []
        started = datetime.now(timezone.utc)
        async for token in self.adapter.complete_stream(request):
            chunks.append(token.text)
            yield token

        content = "".join(chunks)
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        log.append("turn_assistant", {
            "content": content,
            "turn_number": turn_number,
            "model_id": self.adapter.name + ":" + (self.cascade.rungs[0].models or {}).get("default", "claude-sonnet-4-6"),
            "auth_mode": "subscription",
            "estimated_tokens": self.adapter.count_tokens(content),
            "duration_ms": duration_ms,
        })
        # Re-project after each turn.
        all_events = log.read_all()
        project_thread(all_events, thread_id, self.vault_root)

    def list_threads(self) -> list[dict]:
        """List threads from Logs/ directory. Returns [{thread_id, title, updated}, ...]."""
        ...

    def load_thread(self, thread_id: str) -> list[dict]:
        """Load a thread's events as a serialisable list for the plugin."""
        log = EventLog(self._log_path(thread_id))
        return [
            {
                "event_type": e.event_type,
                "timestamp": e.timestamp,
                "payload": e.payload,
            }
            for e in log.read_all()
        ]

    # Helpers
    def _load_history(self, log: EventLog) -> list[NexusMessage]:
        """Read prior turns from the event log and reconstruct as NexusMessages."""
        ...

    def _next_turn_number(self, history: list[NexusMessage]) -> int:
        """Compute the turn number for the next user message."""
        ...
```

Note that `_build_messages` from the original sketch is **gone** — its logic now lives in `orchestrator/prompts.py` as `compose_request_messages`, `build_system_prompt`, and `build_message_history`. This is deliberate: those three functions are where Phase 3's retrieval insertion happens, and isolating them into a pure-function module (no `self`, no I/O) makes the Phase 3 ticket additive rather than a refactor of the orchestrator class.

## `orchestrator/server.py`

```python
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pathlib import Path
import secrets
from .orchestrator import Orchestrator
from .types import StyleProfile

class SendTurnPayload(BaseModel):
    thread_id: str | None = None        # None → create new thread
    user_message: str
    style_profile: StyleProfile | None = None

def build_app(vault_root: Path) -> FastAPI:
    orchestrator = Orchestrator(vault_root)
    token = _load_or_create_token(vault_root)
    app = FastAPI(title="Obsidian Nexus", version="0.1.0")

    def _auth(authorization: str | None) -> None:
        if not authorization or authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="missing/invalid auth")

    @app.post("/turn")
    async def post_turn(payload: SendTurnPayload, authorization: str | None = Header(None)):
        _auth(authorization)
        thread_id = payload.thread_id or orchestrator.new_thread_id()

        async def event_stream():
            yield f'event: thread\ndata: {{"thread_id": "{thread_id}"}}\n\n'
            async for token in orchestrator.send_turn_stream(
                thread_id, payload.user_message, payload.style_profile
            ):
                yield f'event: token\ndata: {token.model_dump_json()}\n\n'
            yield 'event: done\ndata: {}\n\n'

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/threads")
    def list_threads(authorization: str | None = Header(None)):
        _auth(authorization)
        return orchestrator.list_threads()

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str, authorization: str | None = Header(None)):
        _auth(authorization)
        return orchestrator.load_thread(thread_id)

    @app.get("/health")
    async def health():
        # No auth on health.
        h = await orchestrator.adapter.health_check()
        return h.model_dump(mode="json")

    return app

def _load_or_create_token(vault_root: Path) -> str:
    token_path = vault_root / "Orchestrator" / "Config" / "auth.token"
    if token_path.exists():
        return token_path.read_text().strip()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    token_path.write_text(token, encoding="utf-8")
    # Restrict perms on POSIX; on Windows, do nothing.
    try:
        token_path.chmod(0o600)
    except Exception:
        pass
    return token
```

## `orchestrator/__main__.py`

```python
import argparse
import uvicorn
from pathlib import Path
from .server import build_app

def main() -> None:
    parser = argparse.ArgumentParser(prog="nexus")
    parser.add_argument("--vault", type=Path, required=True, help="Path to Obsidian vault root")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app = build_app(args.vault)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
```

## Behaviour rules

1. **Localhost only.** The server binds to `127.0.0.1` by default. Do not accept external connections.
2. **Bearer token auth.** Every plugin request must carry `Authorization: Bearer {token}`. The token is generated on first run, stored at `Orchestrator/Config/auth.token`.
3. **Streaming via SSE.** Server-Sent Events, not WebSockets. Simpler, sufficient for one-way streaming, and well-supported in the browser-like environment Obsidian provides.
4. **First-event payload is the thread id.** The plugin needs it to update its UI before any tokens arrive. Always emit `event: thread` first, even when continuing an existing thread.
5. **No background processes yet.** No hot-reload, no soak watchdog, no health pingers. Phase 1 is request/response only.

## Tests

`tests/test_prompts.py` — pure-function tests, no fixtures needed:

- `build_system_prompt(StyleProfile())` returns a non-empty string containing the three style fields.
- `build_system_prompt` with custom style values reflects them in output.
- `build_message_history([], "hello")` returns `[NexusMessage(role="user", content="hello")]`.
- `build_message_history([prior_user, prior_assistant], "follow-up")` returns three messages in order.
- `compose_request_messages` puts the system message first and the user message last.

`tests/test_orchestrator.py` — use a mocked `AnthropicAdapter`:

- New thread: `send_turn_stream` creates a JSONL file under `Logs/`, with one `turn_user` and one `turn_assistant` event after completion.
- After a turn, the markdown projection exists at `Live/{date}_thread-{id8}_*.md`.
- A second turn on the same thread appends to the same JSONL file (not a new one).
- `load_thread(thread_id)` returns all events in order.
- `list_threads()` returns one entry after one thread is created.
- Phase 1 requires `cascade.yaml`'s rung 1 to be Anthropic; constructing with a different adapter type raises `ValueError`.

`tests/test_server.py` — use FastAPI's `TestClient`:

- `POST /turn` without auth header → 401.
- `POST /turn` with bad token → 401.
- `POST /turn` with valid token streams SSE events: a `thread` event, ≥1 `token` events, a `done` event.
- `GET /threads` lists created threads.
- `GET /health` returns the adapter's health status without requiring auth.

## Definition of done

- [ ] `python -m orchestrator --vault /tmp/test-vault` starts and serves on `127.0.0.1:7777`.
- [ ] `curl -X POST http://127.0.0.1:7777/turn -H "Authorization: Bearer $(cat /tmp/test-vault/Orchestrator/Config/auth.token)" -H 'Content-Type: application/json' -d '{"user_message":"hello"}'` returns an SSE stream from Sonnet 4.6.
- [ ] All tests pass — including `test_prompts.py`.
- [ ] `Orchestrator/Logs/{id8}.jsonl` + `Orchestrator/Live/{...}.md` exist after the curl test.
- [ ] Restarting the orchestrator and re-fetching `GET /threads/{id}` returns the same events.
- [ ] `orchestrator/prompts.py` exists as a standalone pure-function module — no imports from `orchestrator.py`, no side effects, no I/O.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- No cascade engine yet. If the adapter raises, the SSE stream emits an error event and the turn aborts; the orchestrator does NOT try a different model. That's Phase 2.
- No retrieval / RAG. `build_system_prompt` does NOT consume retrieved context yet. The function shape exists for Phase 3 to extend without changing call sites.
- No hot-reload of `cascade.yaml`. Changes require a restart.
- No CORS — only the Obsidian plugin (same-machine) talks to this. No browser-based clients.
