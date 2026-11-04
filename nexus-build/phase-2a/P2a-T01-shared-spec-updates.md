# P2a-T01 — Shared Spec Updates (Trigger Taxonomy + Cascade Contract)

## Goal

Add the contracts every other Phase 2a ticket references. New shared specs for the trigger taxonomy and the cascade engine's API. Update the existing adapter protocol with the new error types and selection_hint semantics.

## Read first

- `nexus-build/CLAUDE.md`
- `nexus-build/shared/adapter-protocol.md`
- `OPEN_QUESTIONS.md` — Phase 1 findings, especially the Ruflo and Claude Code CLI entries

## Files to create or replace

```
nexus-build/shared/trigger-taxonomy.md          ← new
nexus-build/shared/cascade-contract.md          ← new
nexus-build/shared/adapter-protocol.md          ← extend with new error types + selection_hint
```

## `nexus-build/shared/trigger-taxonomy.md`

```markdown
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
```

## `nexus-build/shared/cascade-contract.md`

```markdown
# Cascade Engine Contract

**Status:** Contract. The cascade engine wraps the adapter layer and provides the orchestrator with a single `complete_stream` method that handles failover transparently.

## Responsibilities

The cascade engine owns:

1. Reading `cascade.yaml` and instantiating adapters for each rung.
2. Selecting which rung to ask for each turn (default: highest available rung not currently in an exhausted-cooldown state).
3. Calling the adapter, catching `AdapterError` subclasses, classifying via the trigger taxonomy.
4. On failover: invoking the Haiku summariser, writing the handover doc, marking the failed rung's cooldown state, retrying on the next rung.
5. Hot-reloading `cascade.yaml` when it changes on disk.

The cascade engine does NOT own:

- Vault writes (orchestrator owns those).
- The user-facing model pin (orchestrator stores `pinned_model` in thread YAML; cascade respects it).
- Per-turn Opus selection logic (Phase 2b).

## Interface

```python
from typing import AsyncIterator
from ..types import NexusRequest, Token
from ..adapters.base import Adapter

class CascadeEngine:
    """Wraps adapters with failover. Stateless across requests; cooldowns live in budgets.yaml."""

    def __init__(self, vault_root: Path, config_path: Path):
        ...

    async def complete_stream(
        self,
        request: NexusRequest,
        thread_yaml: dict,    # thread's current YAML frontmatter
    ) -> AsyncIterator[CascadeEvent]:
        """
        Stream tokens with failover. Yields CascadeEvent union:
          - TokenEvent(token: Token)
          - HandoverEvent(from_rung: int, to_rung: int, handover_path: Path, trigger: str)
          - ErrorEvent(message: str, terminal: bool)
        """

    async def health_check_all(self) -> dict[int, HealthStatus]:
        """Health of every configured rung."""

    def select_rung(self, thread_yaml: dict) -> int:
        """Which rung to try first for this thread, respecting pins and cooldowns."""

    async def reload_config(self) -> None:
        """Re-read cascade.yaml. Called by the file watcher."""
```

## CascadeEvent types

```python
from dataclasses import dataclass
from ..types import Token

@dataclass
class TokenEvent:
    token: Token

@dataclass
class HandoverEvent:
    from_rung: int
    to_rung: int
    handover_path: Path           # path to the new handover doc
    trigger: str                   # one of the seven trigger class names

@dataclass
class ErrorEvent:
    message: str
    terminal: bool                 # if True, no more rungs available; abort the turn

CascadeEvent = TokenEvent | HandoverEvent | ErrorEvent
```

## Hot-reload mechanics

The cascade engine watches `Config/cascade.yaml` via `watchdog`. On change:

1. Read and validate the new file.
2. If invalid: log error to `Logs/orchestrator-{date}.log`, do NOT apply. Keep the last good config.
3. If valid: swap in. New requests use the new config; in-flight requests finish on the old adapters.
4. Adapters are re-instantiated on a config change (rungs may have changed).

## Selection rules (Phase 2a default)

`select_rung` returns the lowest-numbered rung position that is:

- Configured in `cascade.yaml`.
- Not currently in a cooldown state (i.e., `Config/budgets.yaml` has no exhausted_until in the future).
- Has a healthy adapter (last health check < 5 minutes old, returned `healthy: true`).

If a `pinned_model` is set on the thread, the cascade looks up which rung serves that model and uses it (ignoring failover, ignoring cooldowns, ignoring health — the user pinned it for a reason; if the model is genuinely unavailable, return `ErrorEvent(terminal=True)`).

Phase 2b will extend `select_rung` with per-turn Opus routing logic. Phase 2a does not.
```

## Updates to `nexus-build/shared/adapter-protocol.md`

Add the following section to the existing file:

```markdown
## Error type hierarchy (Phase 2a additions)

The `AdapterError` subclasses defined in P1-T05 are extended:

- `AdapterError` (base)
  - `AdapterUnavailable` — process failed, network down, auth rejected
  - `AdapterQuotaExhausted` — provider explicitly signaled quota hit
  - `AdapterTimeout` — request exceeded configured timeout
  - `AdapterRefusal` — model returned policy-style refusal
  - `AdapterRateLimit` — HTTP 429 with `Retry-After` (new in Phase 2a)
  - `AdapterContextOverflow` — prompt exceeds adapter's context window (new in Phase 2a)

Each adapter MUST raise the specific subclass, never the base `AdapterError`. The cascade's trigger classifier dispatches on the subclass.

## `selection_hint` field on `NexusRequest`

`NexusRequest` gains an optional `selection_hint: str | None` field used by the cascade engine for routing decisions. Phase 2a uses it only for pin-passthrough (`"pin:gemini-3-1-pro"`). Phase 2b will use it for Opus routing.

```python
class NexusRequest(BaseModel):
    thread_id: str
    messages: list[NexusMessage]
    style_profile: StyleProfile
    model_hint: str | None = None
    selection_hint: str | None = None       # new in Phase 2a
    max_output_tokens: int | None = None
    stream: bool = True
```
```

## Tests

No tests for this ticket — it's spec updates only. Subsequent tickets test against these contracts.

## Definition of done

- [ ] `nexus-build/shared/trigger-taxonomy.md` exists and matches the structure above.
- [ ] `nexus-build/shared/cascade-contract.md` exists.
- [ ] `nexus-build/shared/adapter-protocol.md` has the new error types and `selection_hint` field documented.
- [ ] `OPEN_QUESTIONS.md` does not need new entries (these are PRD-derived).
- [ ] Commit as `docs(phase-2a): trigger taxonomy + cascade contract`.

## Definition of NOT done

- Don't write Python types for the new errors yet — P2a-T05 and P2a-T06 do that.
- Don't update `orchestrator/types.py` for `selection_hint` — that lands with P2a-T08.
- Don't update existing tickets in `phase-1/` retroactively.
