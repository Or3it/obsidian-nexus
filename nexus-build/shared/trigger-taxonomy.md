# Trigger Taxonomy

**Status:** Contract. The cascade engine maps adapter errors to these trigger classes. Tests in P2a-T06 verify each path.

## The seven classes

| Class | Detector | Threshold | Action |
|---|---|---|---|
| `HARD_FAIL` | HTTP 401/403/429 without `Retry-After`, or 5xx after 2 retries | Immediate | Failover, no retry |
| `PRO_QUOTA_EXHAUSTED` | Claude Code stderr contains quota markers, or adapter detects 60s+ hangs on subscription calls | 1 confirmed | Failover to Rung 2; persist `pro_quota_exhausted_until` |
| `SOFT_DEGRADE` | Streaming latency > 3× rolling P95 baseline for 2 consecutive requests | 2 consecutive | Failover with toast |
| `OPENROUTER_QUOTA` | OpenRouter 429 with body containing "quota" or "daily limit" | Immediate | Skip Rungs 3+4 together; failover to Rung 5 |
| `CONTEXT_LIMIT` | Computed prompt size > 90% of active model's window | Per-request | Failover to larger-window rung |
| `EXPLICIT_REFUSAL` | Model returns policy-style refusal when prior identical-context turns succeeded | 1 retry then failover | Failover with user-visible note |
| `MANUAL` | User-initiated via pin or override | Immediate | Honour user's choice |

## Trigger classification rules

- `HARD_FAIL` and `PRO_QUOTA_EXHAUSTED` skip retries. Failover is immediate.
- `SOFT_DEGRADE` permits one retry with exponential backoff (200ms, 400ms) before failing over.
- A failover does NOT reset the retry counter on the rung above. The cascade does not loop back to Rung 1 until either the recorded `pro_quota_exhausted_until` timestamp has passed, or the user manually pins back.
- Adapter exceptions map to triggers in `orchestrator/cascade/triggers.py` (P2a-T06). The mapping is the single source of truth.

## Persistence

When a trigger fires that affects a thread's available rungs (`PRO_QUOTA_EXHAUSTED`, `OPENROUTER_QUOTA`), the orchestrator writes the relevant state to the thread's YAML and to `Config/budgets.yaml`:

```yaml
# In thread YAML
pro_quota_exhausted_until: 2026-05-19T00:00:00Z

# In Config/budgets.yaml (orchestrator-wide)
pro_quota:
  exhausted_until: 2026-05-19T00:00:00Z
  last_seen: 2026-05-13T15:47:12-05:00

openrouter_quota:
  exhausted_until: 2026-05-16T00:00:00Z
  last_seen: 2026-05-15T22:30:00-05:00
```

Phase 2a only writes/reads these fields. Phase 4 surfaces them in the UI.

## Trigger event log

Every trigger firing is appended to the thread's JSONL event log as a `handover` event (per `yaml-schemas.md`) and to `Logs/orchestrator-{date}.log` for debugging.
