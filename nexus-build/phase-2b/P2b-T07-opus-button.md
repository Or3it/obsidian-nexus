# P2b-T07 — Opus toggle button + tool-call indicator

## Goal

1. **Opus toggle button** in the input bar: one-turn-only escalation to Opus without
   the user typing `#opus`. Resets automatically after each send.

2. **Tool-call indicator** in the transcript: consume `tool_call` / `tool_result` SSE
   events (wired in T05) and render a small inline badge so the user can see when
   web search / web fetch is happening.

## Key behaviour — Opus toggle

- Button lives in the input bar: `[textarea] [Opus] [Send]`
- Toggle on → active state (accent styling). Toggle off → muted secondary style.
- On send: `msgToServer = rawInput + " #opus"`. `msgForDisplay = rawInput` (no tag shown).
- `finally` block always resets toggle and re-enables button, even if send fails.
- The existing `_resolve_model` step 1 handles routing — no backend changes needed.
- If the thread is pinned to a non-Anthropic model, `#opus` still takes routing priority
  for that one turn (step 1 beats the pin), then the pin resumes next turn.

## Key behaviour — tool-call indicator

- `tool_call` event → append `{ name, input, done: false }` to `assistantTurn.toolCalls`.
- `tool_result` event → find last matching undone record, set `done: true`.
- Render: small pill below assistant content. `is-pending` (searching…) vs `is-done`
  (search complete, muted). Query string shown for `web_search`; URL domain for `web_fetch`.

## Files to modify

- `nexus/plugin/lib/nexus-client.ts` — add `ToolCallEventData`, `ToolResultEventData` types
- `nexus/plugin/views/chat-view.ts` — opus button, tool-call indicator, send rewiring
- `nexus/plugin/styles.css` — `.nexus-opus-btn`, `.nexus-tool-calls`, `.nexus-tool-call`

## Definition of done

- [ ] Opus button renders in input bar
- [ ] Toggle state is reset in `finally` (never leaks to next turn)
- [ ] `#opus` tag absent from displayed transcript but present in server payload
- [ ] `tool_call` SSE events render a search badge; `tool_result` marks it done
- [ ] `tsc --noEmit` clean; `npm run build` succeeds
- [ ] `main.js` + `styles.css` deployed to `~/nexus-test-vault/.obsidian/plugins/obsidian-nexus/`
- [ ] Committed as `feat(phase-2b): Opus button + tool-call indicator (P2b-T07)`
