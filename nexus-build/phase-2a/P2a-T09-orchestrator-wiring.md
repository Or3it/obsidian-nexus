# P2a-T09 — Orchestrator + Server Wiring for Cascade

## Goal

The cascade engine exists (P2a-T08). The orchestrator still calls the Anthropic adapter directly (Phase 1). This ticket replaces that with cascade-engine calls and extends the SSE stream to emit handover events to the plugin.

## Read first

- `nexus/orchestrator/orchestrator.py` (Phase 1)
- `nexus/orchestrator/server.py` (Phase 1)
- All P2a tickets so far, especially T08

## Files to modify

```
nexus/orchestrator/orchestrator.py
nexus/orchestrator/server.py
nexus/tests/test_orchestrator.py
nexus/tests/test_server.py
```

## Changes to `orchestrator.py`

Phase 1's `Orchestrator.__init__` instantiates a single `AnthropicAdapter`. Replace with a `CascadeEngine`:

```python
from .cascade.engine import CascadeEngine
from .cascade.events import TokenEvent, HandoverEvent, ErrorEvent


class Orchestrator:
    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.cascade = CascadeEngine(
            vault_root=vault_root,
            config_path=vault_root / "Orchestrator" / "Config" / "cascade.yaml",
        )
        self._ensure_vault_layout()

    async def send_turn_stream(
        self,
        thread_id: str,
        user_message: str,
        style_profile: StyleProfile | None = None,
    ) -> AsyncIterator[dict]:
        """
        Append user turn, ask cascade, stream events to caller while
        buffering text; on completion, append assistant turn; project.

        Yields plain dicts the server turns into SSE events:
          - {"type": "token", "text": str, "is_final": bool}
          - {"type": "handover", "from_rung": int, "to_rung": int, "from_model": str, ...}
          - {"type": "error", "message": str, "terminal": bool}
        """
        log = EventLog(self._log_path(thread_id))
        history = self._load_history(log)
        turn_number = self._next_turn_number(history)
        log.append("turn_user", {"content": user_message, "turn_number": turn_number})

        # Read thread YAML for cooldown-aware selection. New thread? Empty dict.
        thread_yaml = self._load_thread_yaml(thread_id) or {}
        style = style_profile or StyleProfile()
        messages = compose_request_messages(history, user_message, style)
        request = NexusRequest(
            thread_id=thread_id,
            messages=messages,
            style_profile=style,
        )

        chunks: list[str] = []
        active_model = "(unknown)"
        live_history_filename = self._derive_live_filename(thread_id, history, user_message)
        started = datetime.now(timezone.utc)

        async for event in self.cascade.complete_stream(
            request=request,
            thread_yaml=thread_yaml,
            live_history_filename=live_history_filename,
        ):
            if isinstance(event, TokenEvent):
                chunks.append(event.token.text)
                yield {"type": "token", "text": event.token.text, "is_final": event.token.is_final}
            elif isinstance(event, HandoverEvent):
                # Log a handover entry in the JSONL event log.
                log.append("handover", {
                    "handover_id": event.handover_path.stem,
                    "from_rung": event.from_rung,
                    "to_rung": event.to_rung,
                    "from_model": event.from_model,
                    "to_model": event.to_model,
                    "trigger": event.trigger,
                })
                active_model = event.to_model
                yield {
                    "type": "handover",
                    "from_rung": event.from_rung,
                    "to_rung": event.to_rung,
                    "from_model": event.from_model,
                    "to_model": event.to_model,
                    "trigger": event.trigger,
                }
            elif isinstance(event, ErrorEvent):
                yield {"type": "error", "message": event.message, "terminal": event.terminal}
                if event.terminal:
                    return

        content = "".join(chunks)
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        log.append("turn_assistant", {
            "content": content,
            "turn_number": turn_number,
            "model_id": active_model,
            "auth_mode": self._auth_mode_for_model(active_model),
            "estimated_tokens": len(content) // 4,
            "duration_ms": duration_ms,
        })
        all_events = log.read_all()
        project_thread(all_events, thread_id, self.vault_root)

    def _load_thread_yaml(self, thread_id: str) -> dict | None:
        """Read the thread's current YAML frontmatter from the projected markdown, if it exists."""
        ...

    def _derive_live_filename(self, thread_id: str, history, user_message: str) -> str:
        """Compute the filename the projection will produce, for use in handover wiki-links."""
        ...

    def _auth_mode_for_model(self, model_id: str) -> str:
        """Inspect the model_id string and map to auth_mode."""
        if model_id.startswith("anthropic:") or "claude-" in model_id:
            return "subscription"
        if model_id.startswith("ollama:"):
            return "local"
        return "api_key_free_tier"
```

## Changes to `server.py`

The Phase 1 server emits `event: thread`, then `event: token`, then `event: done`. Now we also emit `event: handover` and `event: error`. Update the SSE generator:

```python
@app.post("/turn")
async def post_turn(payload: SendTurnPayload, authorization: str | None = Header(None)):
    _auth(authorization)
    thread_id = payload.thread_id or orchestrator.new_thread_id()

    async def event_stream():
        yield f'event: thread\ndata: {{"thread_id": "{thread_id}"}}\n\n'
        try:
            async for ev in orchestrator.send_turn_stream(
                thread_id, payload.user_message, payload.style_profile
            ):
                yield f'event: {ev["type"]}\ndata: {json.dumps(ev, separators=(",", ":"))}\n\n'
                if ev["type"] == "error" and ev.get("terminal"):
                    break
        except Exception as e:
            log.exception("Unhandled error in /turn stream")
            yield f'event: error\ndata: {json.dumps({"type": "error", "message": str(e), "terminal": True})}\n\n'
        yield 'event: done\ndata: {}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Health endpoint now reports cascade-wide health:

```python
@app.get("/health")
async def health():
    rungs = await orchestrator.cascade.health_check_all()
    return {
        "healthy": any(s.healthy for s in rungs.values()),
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rungs": {pos: s.model_dump(mode="json") for pos, s in rungs.items()},
    }
```

The `healthy: true` test in `test_server.py` from Phase 1 still passes as long as Rung 1 is healthy.

## Critical implementation notes

1. **Thread YAML is read from the markdown file each turn.** This is correct (JSONL is canonical; YAML is in the projected markdown). If the markdown doesn't exist yet (new thread), default to empty dict.

2. **Handover event flows to the plugin.** The plugin (P2a-T10) listens for `event: handover` and shows a toast + updates the model pill. The orchestrator's job is just to emit the event.

3. **Active model tracking.** Phase 1 hardcoded `"anthropic:claude-sonnet-4-6"` in the JSONL `model_id` field. Now it follows the handover chain. If no handover happens, default to whatever Rung 1's default model is.

4. **Don't break Phase 1.** The CORS middleware fix from the Phase 1 hot-patch must be preserved. The acceptance criteria for Phase 1 must still pass.

5. **Error events are best-effort.** If the cascade engine yields an ErrorEvent with `terminal: True`, the orchestrator stops the loop and the server emits the error event followed by `done`. The plugin renders the error inline (already does in Phase 1).

## Tests

`tests/test_orchestrator.py` — update Phase 1 tests:

- Replace the mocked `AnthropicAdapter` with a mocked `CascadeEngine` that yields `TokenEvent`s.
- Add: cascade yields a `HandoverEvent` mid-stream → orchestrator writes a `handover` event to the JSONL log.
- Add: cascade yields a terminal `ErrorEvent` → no `turn_assistant` event is written (the turn aborted).
- Add: `_load_thread_yaml` returns the YAML from an existing thread's projected markdown.

`tests/test_server.py` — extend:

- POST /turn with a successful cascade → SSE stream includes `thread`, `token`(s), `done`.
- POST /turn during a failover → SSE stream includes `thread`, `token`(s), `handover`, `token`(s), `done`.
- POST /turn that errors terminally → SSE stream includes `thread`, `error` (terminal), `done`.
- GET /health returns multi-rung structure.

## Definition of done

- [ ] orchestrator.py and server.py updated.
- [ ] All Phase 1 tests still pass.
- [ ] New cascade-event tests pass.
- [ ] Manual smoke test:
  ```bash
  # Start the orchestrator
  python -m orchestrator --vault ~/nexus-test-vault
  
  # In another terminal:
  TOKEN=$(cat ~/nexus-test-vault/Orchestrator/Config/auth.token)
  curl -N -X POST http://127.0.0.1:7777/turn \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"user_message":"Say hi."}'
  ```
  Stream succeeds, Sonnet responds, vault updated.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't update the plugin — P2a-T10.
- Don't add per-turn Opus button or quota indicator. Phase 2b / Phase 4.
- Don't reload thread YAML in real time during streaming — once per turn at the top is enough.
