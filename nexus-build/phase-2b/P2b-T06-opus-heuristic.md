# P2b-T06 — Opus auto-escalation heuristic

## Goal

Add a text-based heuristic that auto-routes complex user messages to Opus 4.7 without
the user typing `#opus`. The heuristic is opt-in via `cascade.yaml` and defaults to
disabled so users on constrained Pro quotas are never surprised.

## Background

Phase 2a shipped `#opus` explicit routing (confirmed working). The auto heuristic was
deferred from Phase 2a. This ticket delivers it as a standalone `heuristics/` module
so it can be tested, tuned, and replaced independently of the adapter.

## Heuristic logic

A message escalates to Opus if **all three** conditions hold:
1. Heuristic is `enabled: true` in `cascade.yaml`
2. The last user message is ≥ `min_chars` (default 500)
3. The last user message contains at least one `keywords` entry (case-insensitive substring)

Default keywords: `design, tradeoff, architecture, review, analyze, analyse, compare,
implement, refactor, strategy, evaluate, critique`

## cascade.yaml schema addition

```yaml
rungs:
  - position: 1
    adapter: anthropic
    ...
    opus_heuristic:
      enabled: true          # default: false
      min_chars: 500         # default: 500
      keywords:              # default list shown above
        - design
        - tradeoff
```

If the `opus_heuristic` block is absent, the adapter behaves exactly as before.

## Priority order after this ticket

1. `#opus` tag in last user message → premium (Opus)
2. `selection_hint="summariser"` → summariser (Haiku) — **heuristic is never reached**
3. `selection_hint="premium"` → premium (Opus)
4. `selection_hint="pin:<model>"` → exact model
5. **Heuristic check** → premium (Opus) if fires
6. default → default_model (Sonnet)

## Files to create

- `nexus/orchestrator/heuristics/__init__.py`
- `nexus/orchestrator/heuristics/opus.py` — `OpusHeuristic` class
- `nexus/tests/test_heuristics_opus.py` — unit tests

## Files to modify

- `nexus/orchestrator/types.py` — add `OpusHeuristicConfig` model; add `opus_heuristic` field to `RungConfig`
- `nexus/orchestrator/adapters/anthropic.py` — accept `opus_heuristic` kwarg; insert step 5 in `_resolve_model`; update docstring
- `nexus/orchestrator/cascade/engine.py` — wire `enable_web_tools` + `opus_heuristic` into `_instantiate_adapter` (fixes T03 oversight for `enable_web_tools`)
- `nexus/tests/test_anthropic_adapter.py` — tests for heuristic routing paths

## Definition of done

- [ ] `OpusHeuristic` in `heuristics/opus.py`, configured from `OpusHeuristicConfig`
- [ ] Absent or `enabled: false` → Sonnet unchanged
- [ ] Long message with matching keyword → Opus when enabled
- [ ] `selection_hint="summariser"` still bypasses heuristic (already guaranteed by priority order)
- [ ] `enable_web_tools` also wired in `_instantiate_adapter` (T03 oversight fix)
- [ ] All 374 prior tests pass; new tests added
- [ ] `mypy orchestrator/adapters/anthropic.py --ignore-missing-imports` clean
- [ ] Committed as `feat(phase-2b): Opus auto-escalation heuristic (P2b-T06)`
