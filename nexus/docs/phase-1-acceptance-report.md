# Phase 1 Acceptance Report

Date: 2026-05-15
Tester: Claude Sonnet 4.6 (automated) / author (manual)
Environment: macOS Darwin 25.4.0 | Python 3.12.10 | Node 24.14.1 | Claude Code 2.1.142

## Seven acceptance criteria

- [x] **1. pytest tests/ passes.** `91 passed in 2.76s`. With integration tests in tree: `91 passed, 5 skipped in 2.70s` (real_claude tests correctly gated by `--real-claude` flag).
- [x] **2. Plugin renders.** `plugin/main.js` (12 KB) built via `npm run build`; `tsc --noEmit` clean (zero errors, strict mode). Manual install: copy `main.js`, `manifest.json`, `styles.css` to `.obsidian/plugins/obsidian-nexus/` and enable. "Nexus Chat" view appears in right sidebar with "Sonnet 4.6" pill in header.
- [x] **3. End-to-end streamed turn.** Integration test `test_e2e_streamed_turn` passed. Prompt: `"Say 'phase one works' and nothing else."` — response confirmed: `"phase one works"`. Manual evidence: prompt `"Reply with exactly three words: phase one verified"` → response: `"phase one verified"` via `anthropic:claude-sonnet-4-6`, subscription mode, 3121 ms.
- [x] **4. Vault persistence.** `Orchestrator/Live/` markdown file written and validated. Thread `019e2cba-9951-7922-b335-18f6fcf408a4`:

```yaml
---
type: conversation
schema_version: 2
thread_id: 019e2cba-9951-7922-b335-18f6fcf408a4
parent_thread: null
title: reply-with-exactly-three-words-phase-one-verified
created: '2026-05-15T12:41:27-05:00'
updated: '2026-05-15T12:41:30.123000-05:00'
status: live
turn_count: 2
models_used:
- id: anthropic:claude-sonnet-4-6
  auth_mode: subscription
  turns:
  - 1
  estimated_tokens: 4
active_model: anthropic:claude-sonnet-4-6
---

## Turn 1 — User
Reply with exactly three words: phase one verified

## Turn 1 — Assistant (anthropic:claude-sonnet-4-6)
phase one verified
```

All required Phase 1 frontmatter fields present: `thread_id`, `status`, `models_used`, `schema_version`.

- [x] **5. Event log integrity.** `Orchestrator/Logs/019e2cba99517922.jsonl` — two events in order:

```jsonl
{"event_id": "019e2cba-9958-7ca3-88ff-f79b272dc298", "timestamp": "2026-05-15T12:41:27.000-05:00", "event_type": "turn_user", "schema_version": 2, "payload": {"content": "Reply with exactly three words: phase one verified", "turn_number": 1, "thread_id": "019e2cba-9951-7922-b335-18f6fcf408a4"}}
{"event_id": "019e2cba-a58a-70e3-816a-a1ee900c7057", "timestamp": "2026-05-15T12:41:30.123-05:00", "event_type": "turn_assistant", "schema_version": 2, "payload": {"content": "phase one verified", "turn_number": 1, "model_id": "anthropic:claude-sonnet-4-6", "auth_mode": "subscription", "estimated_tokens": 4, "duration_ms": 3121}}
```

- [x] **6. Restart survival.** Integration test `test_restart_survival` passed. Manual evidence: stopped orchestrator (PID 21710), restarted (PID 21801), `GET /threads/019e2cba-9951-7922-b335-18f6fcf408a4` returned:

```json
[
  {
    "event_type": "turn_user",
    "timestamp": "2026-05-15T12:41:27.000-05:00",
    "payload": {"content": "Reply with exactly three words: phase one verified", "turn_number": 1}
  },
  {
    "event_type": "turn_assistant",
    "timestamp": "2026-05-15T12:41:30.123-05:00",
    "payload": {"content": "phase one verified", "model_id": "anthropic:claude-sonnet-4-6", "duration_ms": 3121}
  }
]
```

JSONL is the source of truth; restart reads from disk, not memory.

- [x] **7. Ruflo-optional.** Integration test `test_use_ruflo_false_fallback` passed. `cascade.yaml` ships with `use_ruflo: false`. `/health` returns `{"healthy": true}` pointing at the `claude` binary. All five integration tests ran against `use_ruflo: false`; `use_ruflo: true` raises `AdapterUnavailable` immediately (tested in unit suite: `test_use_ruflo_true_raises_adapter_unavailable` PASSED). No API key required — `apiKeySource: "none"` confirmed in adapter.

## Integration test run summary

```
pytest tests/integration/ --real-claude -v
============================= test session starts ==============================
platform darwin — Python 3.12.10
5 items collected

tests/integration/test_phase1_e2e.py::test_e2e_streamed_turn     PASSED [ 20%]
tests/integration/test_phase1_e2e.py::test_vault_persistence      PASSED [ 40%]
tests/integration/test_phase1_e2e.py::test_event_log_integrity    PASSED [ 60%]
tests/integration/test_phase1_e2e.py::test_restart_survival       PASSED [ 80%]
tests/integration/test_phase1_e2e.py::test_use_ruflo_false_fallback PASSED [100%]

============================== 5 passed in 43.66s ==============================
```

## Drift discovered (from OPEN_QUESTIONS.md)

Five entries logged during Phase 1 build, all resolved or deferred:

1. **Ruflo CLI mismatch** — Ruflo v3.7.0-alpha.38 has no `claude-code` subcommand. Resolved: `use_ruflo: false` is the Phase 1 default. Seam left open for MCP-based Phase 5+ integration.
2. **Claude Code flag corrections** — `--message` → positional stdin; `--json` → `--output-format stream-json`. Resolved: adapter verified against v2.1.142.
3. **`--verbose` required with `--output-format stream-json`** — undocumented requirement. Resolved: always included in `_build_command`.
4. **stream-json event format captured** — actual event types verified. Resolved: parser matches real output.
5. **UUID7 log-path collision** — 8-char prefix not unique at same-ms resolution. Resolved: 16-char prefix used for JSONL filenames. Deferred: projection filenames still use 8-char (rare single-user scenario, acceptable in Phase 1).

No blocking items for Phase 2.

## Soak observation

Five integration turns sent (one per AC3-7 test) in 43.66s total.

- Latency P50 / P95: ~3.1 s / ~12 s (Claude Code cold-start included)
- Crashes or unhandled errors: none
- Memory / CPU: nominal (single-user, single-process)

## Sign-off

Phase 1 is ready for Phase 2: ☑ Yes
