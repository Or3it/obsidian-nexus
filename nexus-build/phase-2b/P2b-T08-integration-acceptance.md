# P2b-T08 — Integration acceptance + report

## Goal

Confirm all Phase 2b features work end-to-end against live providers and document the
findings in `docs/phase-2b-acceptance-report.md`.

## Pre-flight checklist

- [ ] Orchestrator healthy (`/health` → all 5 rungs OK)
- [ ] `ANTHROPIC_API_KEY` present in LaunchAgent plist (required for SDK path)
- [ ] `cascade.yaml` has `enable_web_tools: true` and `opus_heuristic.enabled: true` under Rung 1
- [ ] Plugin is the P2b-T07 build (Opus toggle + tool-call pills deployed to test vault)

## Live tests to run

### Test A — ANTHROPIC_API_KEY env confirmation
Verify the live orchestrator process has the key loaded, either via plist inspection or a
test turn that succeeds on the SDK path (no `HARD_FAIL`).

### Test B — Opus heuristic auto-escalation
Send a turn with message >500 chars containing "design" and "tradeoff" keywords.
Expected: `turn_assistant.model_id = "claude-opus-4-7"` in JSONL log.
Negative control: short casual message → Sonnet.

### Test C — Web search SSE
Send "what are the most recent AI model releases in the last week?"
Expected: `event: tool_call` in SSE stream; answer references specific models/dates.

## Files to create/modify

- `nexus/docs/phase-2b-acceptance-report.md` — new, modelled after phase-2a report

## Definition of done

- [ ] All three live tests pass (or blockers documented with root cause + remediation)
- [ ] `pytest tests/` passes (400+)
- [ ] `tsc --noEmit` clean
- [ ] `docs/phase-2b-acceptance-report.md` written
- [ ] Committed as `feat(phase-2b): acceptance`
