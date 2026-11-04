# P2b-T05 — Tool-call event wiring: cascade → orchestrator → SSE

## Goal

Thread tool-call/result visibility from the adapter through the cascade engine to the
plugin via SSE. The plugin can then show a "searching…" indicator when a web tool fires.

## Dependencies

- P2b-T04 (web_fetch tool + registry)
- No new Python packages required

## Design

`Adapter.complete_stream` is typed as `AsyncIterator[Token]`. Changing the protocol would
break all adapters, so tool events are carried as optional metadata on `Token` rather than
a new union type. The cascade engine translates them to proper `CascadeEvent` subtypes.

Flow:
1. `_complete_stream_sdk` yields `Token(tool_call=...)` before dispatch and
   `Token(tool_result=...)` after
2. `CascadeEngine.complete_stream` converts those marker tokens to `ToolCallEvent` /
   `ToolResultEvent`; they are NOT also yielded as `TokenEvent`
3. `Orchestrator.send_turn_stream` adds `ToolCallEvent` and `ToolResultEvent` to its
   isinstance switch; yields `{"type": "tool_call", ...}` / `{"type": "tool_result", ...}`
4. `server.py` already emits `ev["type"]` as the SSE event name — no change needed

## Files to modify

- `nexus/orchestrator/types.py` — add `tool_call: dict[str, Any] | None` and `tool_result: dict[str, Any] | None` to `Token`
- `nexus/orchestrator/cascade/events.py` — add `ToolCallEvent`, `ToolResultEvent`, update `CascadeEvent`
- `nexus/orchestrator/cascade/engine.py` — translate marker tokens in `complete_stream` (both the primary and retry loops)
- `nexus/orchestrator/orchestrator.py` — import + handle two new event types in `send_turn_stream`
- `nexus/orchestrator/adapters/anthropic.py` — emit marker tokens before/after `dispatch_tool`

## Files to modify (tests)

- `nexus/tests/test_cascade_engine.py` — add test: ToolCallEvent/ToolResultEvent yielded from marker tokens
- `nexus/tests/test_orchestrator.py` — add test: `send_turn_stream` yields `tool_call` and `tool_result` dicts
- `nexus/tests/test_anthropic_adapter.py` — add test: `_complete_stream_sdk` emits marker tokens

## SSE payload shapes

```
event: tool_call
data: {"type":"tool_call","name":"web_search","input":{"query":"..."}}

event: tool_result
data: {"type":"tool_result","name":"web_search","content":"[1] ..."}
```

## Definition of done

- [ ] `Token` has `tool_call` and `tool_result` optional fields, default `None`
- [ ] `ToolCallEvent` and `ToolResultEvent` in `cascade/events.py`
- [ ] Engine translates marker tokens in both the primary and retry loop paths
- [ ] `send_turn_stream` yields `tool_call` / `tool_result` dicts
- [ ] Tool-marker tokens are NOT included in `chunks` (assistant content)
- [ ] All 366 prior tests still pass; new tests added
- [ ] `mypy orchestrator/adapters/anthropic.py --ignore-missing-imports` clean
- [ ] Committed as `feat(phase-2b): tool-call event wiring (P2b-T05)`
