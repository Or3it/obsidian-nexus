# YAML Schemas

**Status:** Contract. These are the canonical frontmatter shapes. `frontmatter.py` validates against these.

## Conversation frontmatter (`Live/*.md` and `Archive/**/*.md`)

```yaml
---
type: conversation
schema_version: 2
thread_id: 0192f3b4-1c20-7890-abcd-ef0123456789   # UUIDv7
parent_thread: null                                # null | thread_id of fork parent
title: "Designing the failover cascade"
created: 2026-05-13T14:32:00-05:00
updated: 2026-05-13T15:47:12-05:00
status: live                                       # live | archived | paused | locked
turn_count: 14
pro_quota_consumed_estimate_pct: 12.5
api_spend_usd: 0.00
api_spend_enabled: false
budget_cap_usd: 2.00
models_used:
  - id: claude-sonnet-4-6
    auth_mode: subscription                        # subscription | api_key_free_tier | api_key_paid | local
    turns: [1, 2, 3, 4, 5, 6, 7, 8]
    estimated_tokens: 18000
  - id: claude-opus-4-7
    auth_mode: subscription
    turns: [9]
    estimated_tokens: 4500
  - id: claude-sonnet-4-6
    auth_mode: subscription
    turns: [10, 11, 12, 13, 14]
    estimated_tokens: 9025
active_model: claude-sonnet-4-6
pinned_model: null                                 # null | model-id
jurisdiction_locked: false
pro_quota_exhausted_until: null                    # ISO8601 timestamp when set
style_profile:
  format: prose                                    # prose | structured | mixed
  verbosity: detailed                              # concise | default | detailed
  posture: peer                                    # assistant | peer | tutor
tags: [project/obsidian-nexus, architecture]
related_threads:
  - "[[2026-05-09_thread-aaa11111_initial-brainstorm]]"
related_notes:
  - "[[Project Brief - Obsidian Nexus]]"
handovers: []                                      # list of handover_ids
---
```

### Field rules

- `schema_version`: integer, current = `2`. The validator must refuse any document with `schema_version > 2` and warn on `< 2` (migration path TBD).
- `thread_id`: UUIDv7 (sortable). Generate with a Python lib that supports UUIDv7 (e.g., `uuid-utils`) or implement per RFC 9562.
- `created` / `updated`: ISO 8601 with timezone offset. Always serialize with offset, not just `Z`.
- `models_used`: order matters; each entry is a contiguous run of turns on that model. A model can appear multiple times (returning to it after a failover).
- `style_profile`: the three fields are an enum each; reject unknown values.

### Phase 1 minimum subset

For Phase 1, only these fields are required to be populated (others may be defaulted/null):

- `type`, `schema_version`, `thread_id`, `title`, `created`, `updated`, `status`, `turn_count`, `models_used`, `active_model`, `style_profile`.

The validator must still accept the full schema if present.

---

## Handover frontmatter (`Handovers/*.md`)

Created from Phase 2 onward.

```yaml
---
type: handover
schema_version: 2
handover_id: 01923d4a-9c20-7c12-bbcd-ef0123456abc  # UUIDv7
thread_id: 0192f3b4-1c20-7890-abcd-ef0123456789
sequence: 1                                         # 1-indexed within the thread
from_model: claude-sonnet-4-6
to_model: gemini-3-1-pro
trigger: PRO_QUOTA_EXHAUSTED                        # see trigger taxonomy
trigger_detail: "Pro weekly quota reached; resets 2026-05-19T00:00:00Z"
triggered_at: 2026-05-13T15:47:12-05:00
generated_by: orchestrator                          # orchestrator | outgoing_model
summariser_model: claude-haiku-4-5
tokens_used: 1850
---

## Summary
{≤ 500 words}

## Open Questions
- {item}

## Last User Intent
{one sentence}

## Recommended Continuation Style
{one paragraph}

## Full History Path
[[2026-05-13_thread-a1b2c3d4_designing-cascade]]
```

### Trigger taxonomy (enum for `trigger` field)

- `HARD_FAIL`
- `PRO_QUOTA_EXHAUSTED`
- `SOFT_DEGRADE`
- `OPENROUTER_QUOTA`
- `CONTEXT_LIMIT`
- `EXPLICIT_REFUSAL`
- `MANUAL`

---

## Event log entry (`Logs/{thread-id-short8}.jsonl`)

One JSON object per line, no trailing comma, terminated with `\n`. Atomic append only.

```json
{
  "event_id": "01923d4a-...-...",
  "timestamp": "2026-05-13T14:32:00.123-05:00",
  "event_type": "turn_user",
  "schema_version": 2,
  "payload": { /* event-type-specific */ }
}
```

### Event types

- `turn_user` — payload: `{ "content": str, "turn_number": int }`
- `turn_assistant` — payload: `{ "content": str, "turn_number": int, "model_id": str, "auth_mode": str, "estimated_tokens": int, "duration_ms": int }`
- `handover` — payload: `{ "handover_id": str, "from_model": str, "to_model": str, "trigger": str }`
- `metadata_update` — payload: `{ "field": str, "old": Any, "new": Any }`

Phase 1 only emits `turn_user` and `turn_assistant`. The validator should accept the others as forward-compatible.
