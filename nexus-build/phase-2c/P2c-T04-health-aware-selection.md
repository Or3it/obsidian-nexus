# P2c-T04 — Health-Aware Selection Engine

## Goal

Make the cascade pick the strongest **live** model from the pool, skipping dead and cooled-down ones, and record outcomes into the ModelHealth ledger. This is the routing brain that turns the pool (T02) + health ledger (T03) + correct error classification (T01) into "always the strongest available model, zero manual slug edits."

## Read first

- `nexus/orchestrator/cascade/engine.py` (current select_rung, _cooldown_key, complete_stream loop)
- T02 (PoolConfig / members_by_tier), T03 (ModelHealth), T01 (AdapterModelUnavailable)

## Selection algorithm

When `config.uses_pool`:

```
candidates = [rung-1 Sonnet]                       # subscription, tried first
           + pool.members_by_tier()                # frontier → strong → reliable
           + [floor Ollama]                         # local sovereignty floor

for each candidate in order:
    skip if cooldowns.is_exhausted(cooldown_key(candidate))   # quota / rate-limit
    skip if model_health.is_dead(candidate.model)             # backoff window
    if pinned_model set and candidate.model != pinned_model:  # pin still wins
        continue
    return candidate
# if everything is skipped, fall through to floor unconditionally (better a weak
# answer than none), and log a cascade-degraded warning.
```

Pin interaction: an explicit pin (from the T02 picker / `#opus`) overrides health-aware selection — if the user pinned a model, honor it even if marked dead (they may know it's back). If the pinned model then fails, mark it and fall through to health-aware order.

## Outcome recording

In the streaming loop, on terminal outcome for the chosen model:

```python
try:
    async for token in adapter.complete_stream(...):
        yield ...
    model_health.mark_success(chosen.model)          # streamed cleanly
except AdapterModelUnavailable as e:
    mins = model_health.mark_unavailable(chosen.model, str(e))
    log.info("model %s unavailable, dead %dm: %s", chosen.model, mins, e)
    # cascade to next candidate (existing handover machinery), emit HandoverEvent
except AdapterRateLimit as e:
    cooldowns.mark_exhausted(cooldown_key(chosen), e.cooldown_seconds)
    # do NOT touch model_health failure counter
    # cascade to next candidate
except AdapterQuotaExhausted as e:
    cooldowns.mark_exhausted(e.cooldown_key, QUOTA_COOLDOWN_SECONDS)
    # cascade past all OpenRouter pool members (shared key handles it)
```

The HandoverEvent emitted on each skip should carry the real reason (`model_unavailable` / `rate_limited` / `quota_exhausted`) so T06's UI can show it accurately — this is what finally kills the misleading "rate-limited" message for what is actually a dead model.

## Files to modify

```
nexus/orchestrator/cascade/engine.py          ← pool-aware select + outcome recording
nexus/orchestrator/cascade/events.py          ← HandoverEvent gains a 'reason' field if not present
nexus/tests/test_cascade_engine.py            ← pool selection + outcome tests
```

## Engine changes

- Construct `ModelHealth` in `__init__` alongside `CooldownLedger`, pointed at `Orchestrator/Config/model_health.json`.
- Add `_pool_candidates()` returning the ordered candidate list (Sonnet, pool-by-tier, floor) as lightweight descriptors (adapter + model + cooldown_key).
- `select_candidate(thread_yaml)`: implements the algorithm above. Keep the legacy `select_rung` for schema-2 configs; dispatch on `config.uses_pool`.
- `_cooldown_key`: all pool members on the openrouter adapter share `openrouter_shared` for quota; per-model rate-limits use the model id as the key.
- Wire the three outcome branches into the existing complete_stream cascade loop.

## Reasoning-mode handling

For a pool member with `reasoning_mode: exclude` (e.g. Nemotron), the engine/adapter must request the model in a way that suppresses or budgets the thinking output (per the 2026-05-29 finding that Nemotron writes reasoning into `content` at low token caps). Implementation: pass `reasoning_mode` through to the OpenRouter adapter, which either sets the provider's reasoning-exclude parameter if supported, or raises `max_tokens` headroom so the final answer is reached. Add a small unit test that the parameter is threaded through.

## Tests

`tests/test_cascade_engine.py` (mock adapters + injected ModelHealth/CooldownLedger):
- All pool live → selects the first frontier member after Sonnet (when Sonnet unavailable).
- First two frontier members dead → selects the third live one.
- All frontier dead, strong live → selects strongest strong-tier member.
- Everything dead → falls through to Ollama floor, logs degraded warning.
- `AdapterModelUnavailable` on chosen → ModelHealth.mark_unavailable called, cascades, HandoverEvent.reason == "model_unavailable".
- `AdapterRateLimit` on chosen → cooldown set with the exception's seconds, ModelHealth failure counter untouched, cascades, reason == "rate_limited".
- `AdapterQuotaExhausted` → shared cooldown set, cascades past all pool members, reason == "quota_exhausted".
- Successful stream → ModelHealth.mark_success called for the chosen model.
- Pin honored even when the pinned model is marked dead; on its failure, falls through.
- Legacy schema-2 config still uses select_rung (regression).
- `reasoning_mode` threaded to the adapter for a member that declares it.

## Definition of done

- [ ] Engine selects strongest live pool member; skips dead/cooled correctly.
- [ ] Outcomes recorded: success clears health, model-unavailable drives backoff, rate-limit/quota use cooldown ledger only.
- [ ] HandoverEvent carries an accurate `reason`.
- [ ] Legacy fixed-rung path still works.
- [ ] All tests pass.
- [ ] Manual: with the real schema-3 pool, send turns and confirm via JSONL that a frontier model answers; temporarily point one pool member at a known-bad slug and confirm it's marked dead and skipped on the next turn.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- No proactive probing — T05.
- No task/role routing — T07 (selection here is health + tier only).
- No UI changes — T06.
