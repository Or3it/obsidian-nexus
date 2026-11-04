# Integration Tests — Phase 1

These tests require a live Claude Code installation with an active Pro subscription. They are **not** run by `pytest tests/` and are skipped by default.

## Prerequisites

- Claude Code installed: `claude --version` returns `2.x`
- Authenticated: `claude /login` completed (Pro subscription)
- Python venv active: `source .venv/bin/activate`

## Running

```bash
# From nexus/ root:
pytest tests/integration/ --real-claude -v
```

Or use the smoke script for a quick pass/fail:

```bash
bash scripts/smoke.sh
```

## What the tests cover

| Test | AC | What it verifies |
|---|---|---|
| `test_e2e_streamed_turn` | AC3 | SSE stream delivers thread/token/done events; response text matches prompt |
| `test_vault_persistence` | AC4 | `Orchestrator/Live/*.md` written with valid YAML frontmatter after a turn |
| `test_event_log_integrity` | AC5 | `Orchestrator/Logs/{id16}.jsonl` has ordered turn_user + turn_assistant |
| `test_restart_survival` | AC6 | `GET /threads/{id}` after full stop+restart returns prior events from JSONL |
| `test_use_ruflo_false_fallback` | AC7 | `use_ruflo: false` in cascade.yaml → direct Claude Code invocation succeeds |

AC1 (`pytest tests/` passes) and AC2 (plugin renders) are verified outside this suite.

## Port

Integration tests use port **7778** to avoid conflicting with a running development orchestrator on 7777.
