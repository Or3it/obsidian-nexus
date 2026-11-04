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
