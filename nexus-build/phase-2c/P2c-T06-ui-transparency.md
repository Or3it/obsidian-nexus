# P2c-T06 — UI: Cascade Transparency

## Goal

Make routing visible. The user should see which model answered and, when a cascade happened, which models were skipped and why (dead / rate-limited / quota-exhausted). This is what finally replaces the misleading "rate-limited; brief cooldown" line with an accurate account of what actually happened.

## Read first

- `nexus/plugin/views/chat-view.ts` (renderModelPill, HandoverEvent handling from P2a-T10)
- `nexus/plugin/lib/nexus-client.ts` (SSE event types)
- T04 (HandoverEvent now carries `reason`)

## What to build

1. **Accurate handover line.** When a HandoverEvent arrives, render a compact divider in the affected turn that names the skipped model and the real reason, e.g.:
   - `⤵ nemotron-3-super skipped (unavailable) → gpt-oss-120b`
   - `⤵ minimax skipped (rate-limited 45s) → gpt-oss-120b`
   - `⤵ openrouter quota reached → local (gemma4)`

2. **Routing detail on the pill.** Clicking the model pill (which already opens the picker) gains a small "Routing status" section at the top showing each pool member's live/dead state from `GET /health` (extended in this ticket to include ModelHealth status).

3. **Health endpoint extension.** `GET /health` returns pool member health from the ModelHealth ledger (live / dead-until / reason), so the picker can grey out dead models with a tooltip reason instead of just an opaque dot.

## Files to modify

```
nexus/orchestrator/server.py                 ← /health includes pool ModelHealth status
nexus/orchestrator/orchestrator.py           ← expose model_health.all_status(pool_models)
nexus/plugin/lib/nexus-client.ts             ← HealthResponse type gains pool[]
nexus/plugin/views/chat-view.ts              ← handover line + routing section in picker
nexus/plugin/styles.css
nexus/tests/test_server.py
```

## Server: extend /health

```python
@app.get("/health")
def health(authorization: str | None = Header(None)):
    _auth(authorization)
    base = orchestrator.health_snapshot()        # existing rung health
    if orchestrator.cascade.config.uses_pool:
        pool_models = [m.model for m in orchestrator.cascade.config.pool.members]
        base["pool"] = orchestrator.cascade.model_health.all_status(pool_models)
    return base
```

Each pool entry: `{"model": "...", "state": "live"|"dead"|"untested", "until": iso?, "reason": str?}`.

## Plugin: handover line

In the HandoverEvent handler, map `reason` → label:

```typescript
const REASON_LABEL: Record<string, string> = {
    model_unavailable: "unavailable",
    rate_limited: "rate-limited",
    quota_exhausted: "quota reached",
};
const fromShort = shortName(ho.from_model);   // strip provider prefix
const toShort = shortName(ho.to_model);
const reason = REASON_LABEL[ho.reason] ?? ho.reason ?? "switched";
assistantTurn.handovers.push(`⤵ ${fromShort} skipped (${reason}) → ${toShort}`);
```

Render each handover as a muted divider line above the assistant content (reuse the P2a handover-divider style; just feed it the accurate text).

## Plugin: routing section in picker

When the picker opens, after fetching `/health`, render member rows using `pool[].state`:
- live → normal, selectable, green dot
- dead → dimmed, still selectable (pin overrides health), grey dot, tooltip = `reason` + "retry after {until}"
- untested → normal, neutral dot

## CSS (liquid-glass starter)

Since the dashboard wants a glassy finish later, introduce the token here so it's reusable:

```css
.nexus-glass {
    background: color-mix(in srgb, var(--background-primary) 70%, transparent);
    backdrop-filter: blur(18px) saturate(140%);
    -webkit-backdrop-filter: blur(18px) saturate(140%);
    border: 1px solid color-mix(in srgb, var(--background-modifier-border) 60%, transparent);
    border-radius: 14px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.18);
}
.nexus-handover-line {
    font-size: 0.72em; color: var(--text-muted);
    font-style: italic; padding: 2px 0; opacity: 0.85;
}
.nexus-picker-row.is-dead { opacity: 0.45; }
.nexus-picker-row.is-dead .nexus-picker-health { color: var(--text-faint); }
```

Apply `.nexus-glass` to the picker panel. (Full liquid-glass theming is a later dashboard ticket; this just seeds the shared class.)

## Tests

`tests/test_server.py`:
- `/health` includes a `pool` array when the config uses a pool.
- Each pool entry has `model` and `state`; dead entries include `until` and `reason`.
- Legacy schema-2 config: `/health` has no `pool` key (backward compat).

Plugin: manual verification (no TS test harness in this repo) — covered in the DoD.

## Definition of done

- [ ] `/health` returns pool ModelHealth status; legacy configs unaffected.
- [ ] Handover lines name the real reason (unavailable / rate-limited / quota reached), not a generic message.
- [ ] Picker greys out dead models with a tooltip reason.
- [ ] `.nexus-glass` class added and applied to the picker panel.
- [ ] Server tests pass.
- [ ] `tsc --noEmit` clean; `npm run build`; main.js + styles.css copied to `~/nexus-test-vault/.obsidian/plugins/obsidian-nexus/`.
- [ ] Manual: force a cascade, confirm the handover line shows the accurate reason and the picker shows the dead model dimmed with a tooltip.

## Definition of NOT done

- Not the full dashboard / liquid-glass theme — just the shared class + accurate handover UI.
- No per-token cost display — Phase 4.
- No task-routing indicator — T07.
