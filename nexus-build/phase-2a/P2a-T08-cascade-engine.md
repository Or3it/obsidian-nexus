# P2a-T08 — Cascade Engine (Selection, Failover, Hot-Reload)

## Goal

The cascade engine itself. Takes a NexusRequest, picks a rung, calls the adapter, catches errors, classifies them via the trigger taxonomy, generates a handover if failing over, retries on the next rung, yields events back to the orchestrator. Plus hot-reloading `cascade.yaml`.

This is the largest Phase 2a ticket. Budget the most time.

## Read first

- `shared/cascade-contract.md` (the full contract)
- `shared/trigger-taxonomy.md`
- All P2a tickets T01–T07 (this composes them)
- P1-T06's orchestrator.py — the cascade engine slots into where T06 currently calls the adapter directly

## Files to create

```
nexus/orchestrator/cascade/engine.py
nexus/orchestrator/cascade/events.py
nexus/orchestrator/cascade/cooldown.py
nexus/orchestrator/cascade/config_watcher.py
nexus/tests/test_cascade_engine.py
nexus/tests/test_cooldown.py
nexus/tests/test_config_watcher.py
```

Add to `pyproject.toml`:

```toml
"watchdog>=4.0",
```

## `nexus/orchestrator/cascade/events.py`

```python
from dataclasses import dataclass
from pathlib import Path
from ..types import Token


@dataclass
class TokenEvent:
    token: Token


@dataclass
class HandoverEvent:
    from_rung: int
    to_rung: int
    from_model: str
    to_model: str
    handover_path: Path
    trigger: str


@dataclass
class ErrorEvent:
    message: str
    terminal: bool


CascadeEvent = TokenEvent | HandoverEvent | ErrorEvent
```

## `nexus/orchestrator/cascade/cooldown.py`

```python
"""
Rung cooldown state. Persisted to Config/budgets.yaml so quota exhaustion
survives orchestrator restarts.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import yaml


class CooldownLedger:
    """Tracks per-rung cooldowns. Stateful; reads/writes Config/budgets.yaml."""

    def __init__(self, budgets_path: Path):
        self.path = budgets_path
        self._state: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._state, f, sort_keys=False, allow_unicode=True)

    def mark_exhausted(self, key: str, seconds: int) -> None:
        """key is a rung identifier, e.g. 'rung_1_anthropic' or 'openrouter_shared'."""
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        self._state[key] = {
            "exhausted_until": until.isoformat(timespec="seconds"),
            "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._save()

    def is_exhausted(self, key: str) -> bool:
        record = self._state.get(key)
        if not record:
            return False
        until = datetime.fromisoformat(record["exhausted_until"])
        return datetime.now(timezone.utc) < until

    def clear(self, key: str) -> None:
        if key in self._state:
            del self._state[key]
            self._save()
```

## `nexus/orchestrator/cascade/config_watcher.py`

```python
"""
File watcher for Config/cascade.yaml. Calls a reload callback on changes.
"""

from pathlib import Path
from typing import Callable
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class CascadeConfigWatcher:
    def __init__(self, config_path: Path, on_change: Callable[[], None]):
        self.config_path = config_path.resolve()
        self.on_change = on_change
        self._observer: Observer | None = None
        self._debounce_timer: threading.Timer | None = None

    def start(self) -> None:
        if self._observer:
            return
        handler = self._Handler(self.config_path, self._debounced_change)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.config_path.parent), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    def _debounced_change(self) -> None:
        # Editors save in multiple writes; debounce 500ms.
        if self._debounce_timer:
            self._debounce_timer.cancel()
        self._debounce_timer = threading.Timer(0.5, self.on_change)
        self._debounce_timer.start()

    class _Handler(FileSystemEventHandler):
        def __init__(self, target: Path, callback: Callable[[], None]):
            self.target = target
            self.callback = callback

        def on_modified(self, event):
            if Path(event.src_path).resolve() == self.target:
                self.callback()
```

## `nexus/orchestrator/cascade/engine.py`

```python
"""
Cascade engine. Wraps adapters with failover, handover generation, and hot-reload.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ..adapters.base import Adapter, AdapterError
from ..adapters.anthropic import AnthropicAdapter
from ..adapters.google import GoogleAdapter
from ..adapters.openrouter import OpenRouterAdapter
from ..adapters.ollama import OllamaAdapter
from ..config import load_cascade, CascadeConfig
from ..types import NexusRequest, NexusMessage, Token
from .events import CascadeEvent, TokenEvent, HandoverEvent, ErrorEvent
from .triggers import classify_error, manual_trigger, LatencyTracker, TriggerOutcome
from .summariser import Summariser
from .handover import generate_handover, format_handover_for_prompt
from .cooldown import CooldownLedger
from .config_watcher import CascadeConfigWatcher


log = logging.getLogger(__name__)


class CascadeEngine:
    def __init__(self, vault_root: Path, config_path: Path):
        self.vault_root = vault_root
        self.config_path = config_path
        self.budgets_path = vault_root / "Orchestrator" / "Config" / "budgets.yaml"
        self.cooldowns = CooldownLedger(self.budgets_path)
        self.latency = LatencyTracker()
        self._config: CascadeConfig | None = None
        self._adapters: dict[int, Adapter] = {}
        self._summariser: Summariser | None = None
        self._reload_lock = asyncio.Lock()
        self._watcher = CascadeConfigWatcher(self.config_path, self._on_config_changed)
        self._load_config()
        self._watcher.start()

    # --- Configuration ---

    def _load_config(self) -> None:
        self._config = load_cascade(self.config_path)
        self._adapters = {}
        for rung_cfg in self._config.rungs:
            adapter = self._instantiate_adapter(rung_cfg)
            self._adapters[rung_cfg.position] = adapter
            if rung_cfg.adapter == "anthropic":
                self._summariser = Summariser(adapter)
        if not self._summariser:
            log.warning(
                "No Anthropic adapter in cascade — summariser will fall back to "
                "the lowest-rung available model for handover generation."
            )

    def _instantiate_adapter(self, rung_cfg) -> Adapter:
        if rung_cfg.adapter == "anthropic":
            models = rung_cfg.models or {}
            return AnthropicAdapter(
                default_model=models.get("default", "claude-sonnet-4-6"),
                premium_model=models.get("premium", "claude-opus-4-7"),
                summariser_model=models.get("summariser", "claude-haiku-4-5"),
                use_ruflo=rung_cfg.use_ruflo,
                ruflo_version=rung_cfg.ruflo_version,
            )
        if rung_cfg.adapter == "google":
            return GoogleAdapter(model=rung_cfg.model or "gemini-3-1-pro")
        if rung_cfg.adapter == "openrouter":
            return OpenRouterAdapter(model=rung_cfg.model or "")
        if rung_cfg.adapter == "ollama":
            return OllamaAdapter(model=rung_cfg.model or "gemma4:latest")
        raise ValueError(f"Unknown adapter: {rung_cfg.adapter}")

    def _on_config_changed(self) -> None:
        # Watcher runs on a thread; schedule the actual reload on the orchestrator's loop.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            log.warning("Config changed but no event loop available to reload.")
            return
        asyncio.run_coroutine_threadsafe(self.reload_config(), loop)

    async def reload_config(self) -> None:
        async with self._reload_lock:
            try:
                new_config = load_cascade(self.config_path)
                log.info("cascade.yaml reloaded; %d rungs", len(new_config.rungs))
                self._load_config()
            except Exception as e:
                log.error("Invalid cascade.yaml — keeping previous config. Error: %s", e)

    # --- Selection ---

    def _cooldown_key(self, position: int) -> str:
        rung_cfg = self._config.rungs[position - 1]
        if rung_cfg.adapter == "openrouter":
            return "openrouter_shared"
        return f"rung_{position}_{rung_cfg.adapter}"

    def select_rung(self, thread_yaml: dict) -> int:
        """Lowest position with a non-cooled-down adapter."""
        pinned = thread_yaml.get("pinned_model")
        if pinned:
            for rung_cfg in self._config.rungs:
                models = rung_cfg.models or {}
                if pinned in (rung_cfg.model, models.get("default"), models.get("premium"), models.get("summariser")):
                    return rung_cfg.position
        for rung_cfg in self._config.rungs:
            if not self.cooldowns.is_exhausted(self._cooldown_key(rung_cfg.position)):
                return rung_cfg.position
        # All cooled down — return Rung 5 anyway, last hope
        return self._config.rungs[-1].position

    # --- Streaming with failover ---

    async def complete_stream(
        self,
        request: NexusRequest,
        thread_yaml: dict,
        live_history_filename: str,
    ) -> AsyncIterator[CascadeEvent]:
        """
        Main entrypoint. Yields TokenEvent / HandoverEvent / ErrorEvent.
        The orchestrator (P2a-T09) is what actually consumes these and writes to the vault.
        """
        rung_pos = self.select_rung(thread_yaml)
        handover_context: str | None = None        # set when we failover
        sequence = len(thread_yaml.get("handovers", []))

        while True:
            adapter = self._adapters.get(rung_pos)
            if not adapter:
                yield ErrorEvent(message=f"No adapter for rung {rung_pos}", terminal=True)
                return

            patched_request = self._inject_handover(request, handover_context)
            started = datetime.now(timezone.utc)
            try:
                async for token in adapter.complete_stream(patched_request):
                    yield TokenEvent(token=token)
                duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
                self.latency.record(adapter.name, duration_ms)
                if self.latency.is_degrading(adapter.name, duration_ms):
                    log.warning("Adapter %s degrading; flagging soft failover", adapter.name)
                    # Failover next time, but don't fail this completed call.
                return
            except AdapterError as exc:
                outcome = classify_error(exc, adapter.name)
                log.info("Trigger %s on rung %d: %s", outcome.trigger.value, rung_pos, outcome.detail)
                if outcome.cooldown_seconds:
                    self.cooldowns.mark_exhausted(
                        self._cooldown_key(rung_pos),
                        outcome.cooldown_seconds,
                    )
                    if outcome.cross_rung_cooldown:
                        # OpenRouter pool — both rungs marked exhausted.
                        # The cooldown_key for OpenRouter is shared already; nothing extra needed
                        # since both rung 3 and rung 4 use openrouter_shared.
                        pass

                if outcome.retryable:
                    # one retry on the same rung
                    try:
                        async for token in adapter.complete_stream(patched_request):
                            yield TokenEvent(token=token)
                        return
                    except AdapterError as retry_exc:
                        outcome = classify_error(retry_exc, adapter.name)
                        if outcome.cooldown_seconds:
                            self.cooldowns.mark_exhausted(
                                self._cooldown_key(rung_pos),
                                outcome.cooldown_seconds,
                            )

                if not outcome.failover:
                    yield ErrorEvent(message=outcome.detail, terminal=True)
                    return

                # Generate handover and move to next rung.
                next_rung_pos = self._next_rung_position(rung_pos)
                if next_rung_pos is None:
                    yield ErrorEvent(
                        message="All rungs exhausted; no fallback available.",
                        terminal=True,
                    )
                    return

                sequence += 1
                from_model = self._active_model_for_rung(rung_pos, request)
                to_model = self._active_model_for_rung(next_rung_pos, request)
                handover_path = await self._generate_handover_safe(
                    thread_id=request.thread_id,
                    sequence=sequence,
                    from_model=from_model,
                    to_model=to_model,
                    trigger_outcome=outcome,
                    transcript=request.messages,
                    style_profile=request.style_profile,
                    full_history_filename=live_history_filename,
                )
                yield HandoverEvent(
                    from_rung=rung_pos,
                    to_rung=next_rung_pos,
                    from_model=from_model,
                    to_model=to_model,
                    handover_path=handover_path,
                    trigger=outcome.trigger.value,
                )
                handover_context = self._read_handover_for_prompt(handover_path)
                rung_pos = next_rung_pos
                continue

    def _inject_handover(self, request: NexusRequest, handover_context: str | None) -> NexusRequest:
        if not handover_context:
            return request
        # Prepend handover context to the first system message, or insert one.
        messages = list(request.messages)
        if messages and messages[0].role == "system":
            new_system = NexusMessage(
                role="system",
                content=handover_context + "\n\n" + messages[0].content,
            )
            messages[0] = new_system
        else:
            messages.insert(0, NexusMessage(role="system", content=handover_context))
        return request.model_copy(update={"messages": messages})

    async def _generate_handover_safe(self, **kwargs) -> Path:
        """
        Generate a handover doc. Falls back to a templated handover if Haiku also fails.
        """
        if self._summariser:
            try:
                return await generate_handover(
                    summariser=self._summariser,
                    vault_root=self.vault_root,
                    summariser_model_id="claude-haiku-4-5",
                    **kwargs,
                )
            except AdapterError as e:
                log.warning("Haiku summariser failed (%s); using fallback template", e)
        # Fallback: synthesise a handover from the transcript without a model call.
        return self._fallback_handover(**kwargs)

    def _fallback_handover(self, **kwargs) -> Path:
        """Synthesise a minimal handover doc without calling any model."""
        ...

    def _active_model_for_rung(self, position: int, request: NexusRequest) -> str:
        rung_cfg = self._config.rungs[position - 1]
        if rung_cfg.adapter == "anthropic":
            models = rung_cfg.models or {}
            return models.get("default", "claude-sonnet-4-6")
        return rung_cfg.model or rung_cfg.adapter

    def _next_rung_position(self, current: int) -> int | None:
        for rung_cfg in self._config.rungs:
            if rung_cfg.position > current:
                if not self.cooldowns.is_exhausted(self._cooldown_key(rung_cfg.position)):
                    return rung_cfg.position
                continue
        return None

    def _read_handover_for_prompt(self, path: Path) -> str:
        from .handover import load_handover
        handover = load_handover(path)
        return format_handover_for_prompt(handover)

    # --- Health ---

    async def health_check_all(self) -> dict[int, "HealthStatus"]:
        results = {}
        for pos, adapter in self._adapters.items():
            results[pos] = await adapter.health_check()
        return results

    def shutdown(self) -> None:
        self._watcher.stop()
```

## Critical implementation notes

1. **The engine is the *only* failover authority.** Adapters raise errors; the engine classifies, retries, fails over. No adapter should ever try to be clever about retrying — that's the engine's job.

2. **Hot-reload is debounced.** Editors write configs in multiple steps (truncate, write, rename). The 500ms debounce prevents reload-storms.

3. **The reload validates before applying.** If the new `cascade.yaml` is invalid, `load_cascade` raises `ConfigError`. The engine catches it, logs, keeps the previous config. The user sees no disruption.

4. **`_inject_handover` modifies the request immutably.** Pydantic's `model_copy` ensures the original request is unchanged. The next rung's adapter sees the augmented system prompt; the original `request.messages` list isn't mutated.

5. **The fallback handover path is critical.** If Haiku is unavailable (rare but possible — Anthropic outage), the cascade must still produce a handover. A templated fallback that extracts the last user turn and a short summary from the transcript (no model call) is acceptable. Phase 2a ships this; Phase 4 could make it smarter.

6. **OpenRouter cross-rung cooldown is handled by using a shared cooldown key.** Both Rung 3 and Rung 4 OpenRouter instances use `openrouter_shared` as the key. When either triggers `OPENROUTER_QUOTA`, both are immediately marked exhausted.

7. **The engine writes to budgets.yaml directly.** That file is hot-reloaded by Phase 4's UI; in Phase 2a it's write-mostly.

## Tests

`tests/test_cooldown.py`:

- Mark exhausted with 10s cooldown → `is_exhausted` returns True immediately.
- After 11 wall-clock seconds, `is_exhausted` returns False. (Use `freezegun` or monkey-patch `datetime.now` to avoid actually sleeping.)
- `clear` removes the entry.
- State persists across `CooldownLedger` instantiations against the same file.

`tests/test_config_watcher.py`:

- Modifying the watched file triggers the callback after the debounce.
- Multiple modifications within 500ms trigger only one callback.
- Modifying a different file doesn't trigger.
- `stop()` halts the watcher.

`tests/test_cascade_engine.py` — use mocked adapters:

- Successful Rung 1 call streams tokens with no HandoverEvent.
- Rung 1 raises `AdapterQuotaExhausted` → engine emits HandoverEvent and continues on Rung 2.
- After the failover, the cooldown ledger has Rung 1 marked exhausted.
- Rung 1 cooled-down → engine starts on Rung 2 directly (no handover, since the cascade never tried Rung 1).
- `selection_hint` from `pinned_model` overrides cooldowns.
- All rungs exhausted → engine yields a terminal ErrorEvent.
- OpenRouter Rung 3 raising `AdapterQuotaExhausted` cools down Rung 4 too.
- Hot-reload: rewrite `cascade.yaml` → after debounce, the engine reloads.
- Invalid hot-reload: rewrite with bad YAML → engine keeps previous config.

## Definition of done

- [ ] All four cascade/ files exist.
- [ ] All tests pass (~30 new tests).
- [ ] Manual smoke test: run the orchestrator with the cascade engine wired in. Send a turn. Edit cascade.yaml to put Rung 1 in a definitely-broken state (bad model string). Send another turn. Observe failover to Rung 2 with a real handover doc in `Orchestrator/Handovers/`.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't wire to the orchestrator/server — that's P2a-T09.
- Don't update the plugin — P2a-T10.
- Don't persist the latency tracker across restarts (intentional).
- Don't implement model-aware Pro quota tracking. Phase 4.
