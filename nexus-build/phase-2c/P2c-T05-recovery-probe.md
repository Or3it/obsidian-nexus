# P2c-T05 — Just-In-Time Recovery Probe (Optional, Quota-Sane)

## Goal

Close the one rough edge of pure-lazy health: when a previously-dead top-tier model's backoff expires, the next turn that selects it might still eat a streaming failure if the provider is still down. Add a single, optional 1-token probe fired **only** at the moment such a model is about to be promoted back — so the user doesn't see a mid-stream failure on the recovery edge. This is the one place a deep health check is allowed, and it costs at most one cheap call per recovery event.

## Why this is quota-sane

Continuous probing is impossible on the free tier (~200 req/day shared). This probe fires only when ALL of these hold:
- The candidate's `dead_until` has just expired (it's eligible again after backoff), AND
- It is the model about to be selected (i.e. it's the strongest live candidate), AND
- `pool.recovery_probe: true` in config (default true; can be disabled).

So in steady state (models healthy) it never fires. It fires at most once per model per backoff cycle, on the transition from dead→eligible. Worst case a handful of calls/day.

## Read first

- T01 (`health_check(deep=True)`), T03 (ModelHealth), T04 (select_candidate)

## Files to modify

```
nexus/orchestrator/types.py              ← PoolConfig gains recovery_probe: bool = True
nexus/orchestrator/cascade/engine.py     ← probe-on-promotion in select path
nexus/tests/test_cascade_engine.py
```

## Behaviour

In `select_candidate`, when the strongest eligible candidate is a pool member whose ModelHealth shows it was dead and just became eligible (failure counter > 0 but `dead_until` now in the past), and `recovery_probe` is on:

```python
if needs_recovery_probe(candidate):
    status = await adapter_for(candidate).health_check(deep=True)
    if status.healthy:
        model_health.mark_success(candidate.model)   # confirmed back
        return candidate
    else:
        mins = model_health.mark_unavailable(candidate.model, status.detail)
        log.info("recovery probe failed for %s, dead %dm", candidate.model, mins)
        continue   # try the next candidate
```

`needs_recovery_probe` returns True only when the entry has `consecutive_failures > 0` (it has failed before) and is currently eligible (not dead) — i.e. it just came back from a backoff window. A model that has never failed is never probed.

Guard against probe storms: cache the probe result for a short window (e.g. 60s) so two near-simultaneous turns don't both probe the same model.

## Config

```yaml
pool:
  recovery_probe: true        # default; set false to rely purely on lazy mark-on-failure
  ...
```

## Tests

`tests/test_cascade_engine.py`:
- Candidate never failed → no probe, selected directly.
- Candidate failed then backoff expired, probe returns healthy → mark_success, selected.
- Candidate failed then backoff expired, probe returns unhealthy → mark_unavailable, next candidate chosen.
- `recovery_probe: false` → no probe even on the recovery edge; candidate selected optimistically (lazy mode).
- Probe result cached within the dedupe window (second call in the window does not re-probe).

## Definition of done

- [ ] `recovery_probe` config flag (default true).
- [ ] Probe fires only on the dead→eligible promotion edge for previously-failed pool members.
- [ ] Probe result short-cached to prevent storms.
- [ ] All tests pass.
- [ ] Manual: force a pool member dead, advance past backoff, confirm exactly one probe fires on the next selection and the model is either confirmed back or re-marked.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- No periodic/background sweep — strictly on-promotion only.
- No probing of healthy or never-failed models.
- No deep probe in the hot path for models that are currently live.
