"""Cascade engine: failover, handover generation, hot-reload.

This is the single failover authority. Adapters raise AdapterError subclasses;
the engine classifies them, retries, generates handover docs, and moves to the
next rung. No adapter should retry or fail-over on its own.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uuid_utils as uuid
import yaml

from ..adapters.anthropic import AnthropicAdapter
from ..adapters.base import (
    Adapter,
    AdapterError,
    AdapterModelUnavailable,
    AdapterQuotaExhausted,
    AdapterRateLimit,
)
from ..adapters.ollama import OllamaAdapter
from ..adapters.openrouter import OpenRouterAdapter
from ..config import ConfigError, load_cascade
from ..heuristics.task_classifier import (
    Classification,
    classify_heuristic,
    classify_ollama,
)
from ..prompts import RESEARCH_FALLBACK_NOTICE
from ..types import (
    CascadeConfig,
    NexusMessage,
    NexusRequest,
    RungConfig,
    StyleProfile,
    Token,
)
from .config_watcher import CascadeConfigWatcher
from .cooldown import CooldownLedger
from .events import (
    CascadeEvent,
    ClassificationEvent,
    ErrorEvent,
    HandoverEvent,
    MetaEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from .handover import format_handover_for_prompt, generate_handover, load_handover
from .model_health import ModelHealth
from .summariser import Summariser
from .triggers import LatencyTracker, TriggerClass, TriggerOutcome, classify_error

log = logging.getLogger(__name__)

_QUOTA_COOLDOWN_SECONDS = 3600   # conservative 1-hour cooldown when daily quota fires
_PROBE_CACHE_TTL_SECONDS = 60    # prevent probe storms: dedupe window for recovery probes

# Auto-mode resolution: task classifier class → effective turn mode (Phase 3W).
# This supersedes the old route_by_role switch.
_AUTO_MODE_MAP: dict[str, str] = {
    "conversational": "light",
    "general": "light",
    "coding": "hard",
    "research": "research",
}


@dataclass
class _CandidateInfo:
    """Lightweight descriptor for one candidate in health-aware pool selection."""

    adapter: Adapter
    model: str            # slug for ModelHealth + per-model rate-limit cooldown key
    quota_key: str | None  # "openrouter_shared" for OR pool members; None for Sonnet/Ollama
    reasoning_mode: str | None = None  # "exclude" → add reasoning-exclusion payload param
    idx: int = 0          # position in the ordered candidate list (for HandoverEvent)
    roles: list[str] = field(default_factory=list)  # from PoolMember.roles (for role-bias)
    tier: str = ""        # from PoolMember.tier ("" for rung-1 / floor)
    kind: str = "pool"    # "rung1" | "pool" | "floor"


class CascadeEngine:
    """Wraps adapters with failover. Cooldowns live in budgets.yaml."""

    def __init__(self, vault_root: Path, config_path: Path) -> None:
        self.vault_root = vault_root
        self.config_path = config_path
        self.budgets_path = vault_root / "Orchestrator" / "Config" / "budgets.yaml"
        self.cooldowns = CooldownLedger(self.budgets_path)
        self.model_health = ModelHealth(
            vault_root / "Orchestrator" / "Config" / "model_health.json"
        )
        self.latency = LatencyTracker()
        self._adapters: dict[int, Adapter] = {}
        self._pool_adapters: dict[str, Adapter] = {}
        self._floor_adapter: Adapter | None = None
        self._probe_cache: dict[str, datetime] = {}  # model → cache expiry (probe storm guard)
        self._summariser: Summariser | None = None
        self._reload_lock = asyncio.Lock()

        # Loop is captured lazily on the first async call — at __init__ time the
        # uvicorn event loop has not started yet, so get_running_loop() raises.
        self._loop: asyncio.AbstractEventLoop | None = None

        # Load initial config — raises ConfigError if invalid.
        self._config: CascadeConfig = load_cascade(config_path)
        self._apply_config(self._config)

        self._watcher = CascadeConfigWatcher(config_path, self._on_config_changed)
        self._watcher.start()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _apply_config(self, config: CascadeConfig) -> None:
        self._config = config
        self._adapters = {}
        self._pool_adapters = {}
        self._floor_adapter = None
        self._probe_cache = {}
        self._summariser = None
        for rung_cfg in config.rungs:
            adapter = self._instantiate_adapter(rung_cfg)
            self._adapters[rung_cfg.position] = adapter
            if rung_cfg.adapter == "anthropic" and isinstance(adapter, AnthropicAdapter):
                self._summariser = Summariser(adapter)
        if config.pool is not None:
            for member in config.pool.members:
                try:
                    self._pool_adapters[member.model] = OpenRouterAdapter(
                        model=member.model,
                        reasoning_mode=member.reasoning_mode,
                    )
                except ValueError as exc:
                    log.warning("Cannot instantiate pool member %s: %s", member.model, exc)
        if config.floor is not None and config.floor.adapter == "ollama":
            self._floor_adapter = OllamaAdapter(model=config.floor.model)
        if config.classifier.route_by_role:
            log.warning(
                "classifier.route_by_role is superseded by Phase 3W mode-based "
                "routing and is ignored — remove it from cascade.yaml."
            )
        if self._summariser is None:
            log.warning(
                "No Anthropic adapter in cascade — handover fallback template will be used."
            )

    def _instantiate_adapter(self, rung_cfg: RungConfig) -> Adapter:
        if rung_cfg.adapter == "anthropic":
            models = rung_cfg.models or {}
            heuristic = None
            if rung_cfg.opus_heuristic is not None:
                from ..heuristics.opus import OpusHeuristic  # noqa: PLC0415
                heuristic = OpusHeuristic(rung_cfg.opus_heuristic)
            return AnthropicAdapter(
                default_model=models.get("default", "claude-sonnet-4-6"),
                premium_model=models.get("premium", "claude-opus-4-7"),
                summariser_model=models.get("summariser", "claude-haiku-4-5"),
                use_ruflo=rung_cfg.use_ruflo,
                ruflo_version=rung_cfg.ruflo_version,
                enable_web_tools=rung_cfg.enable_web_tools,
                opus_heuristic=heuristic,
            )
        if rung_cfg.adapter == "openrouter":
            return OpenRouterAdapter(model=rung_cfg.model or "")
        if rung_cfg.adapter == "ollama":
            return OllamaAdapter(model=rung_cfg.model or "gemma4:latest")
        if rung_cfg.adapter == "google":
            from ..adapters.google import GoogleAdapter
            return GoogleAdapter(model=rung_cfg.model or "gemini-3.1-pro-preview")
        raise ConfigError(f"Unknown adapter type: {rung_cfg.adapter!r}")

    def _on_config_changed(self) -> None:
        """Called from the watchdog thread on cascade.yaml file change."""
        loop = self._loop
        if loop is None or not loop.is_running():
            log.warning("cascade.yaml changed but no running event loop to reload on.")
            return
        asyncio.run_coroutine_threadsafe(self.reload_config(), loop)

    def _capture_loop_if_needed(self) -> None:
        """Lazy loop capture — called from the first async entry point."""
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

    async def reload_config(self) -> None:
        """Re-read and validate cascade.yaml. Keeps previous config on failure."""
        async with self._reload_lock:
            try:
                new_config = load_cascade(self.config_path)
                self._apply_config(new_config)
                log.info("cascade.yaml reloaded; %d rungs", len(new_config.rungs))
            except Exception as e:
                log.error("Invalid cascade.yaml — keeping previous config. Error: %s", e)

    # ------------------------------------------------------------------
    # Rung selection
    # ------------------------------------------------------------------

    def _cooldown_key(self, position: int) -> str:
        for rung_cfg in self._config.rungs:
            if rung_cfg.position == position:
                if rung_cfg.adapter == "openrouter":
                    return "openrouter_shared"
                return f"rung_{position}_{rung_cfg.adapter}"
        return f"rung_{position}_unknown"

    def select_rung(self, thread_yaml: dict[str, Any]) -> int:
        """Lowest position with a non-cooled-down adapter, respecting pinned_model."""
        pinned = thread_yaml.get("pinned_model")
        if pinned:
            for rung_cfg in self._config.rungs:
                models = rung_cfg.models or {}
                candidates = (
                    rung_cfg.model,
                    models.get("default"),
                    models.get("premium"),
                    models.get("summariser"),
                )
                if pinned in candidates:
                    return rung_cfg.position

        for rung_cfg in sorted(self._config.rungs, key=lambda r: r.position):
            if not self.cooldowns.is_exhausted(self._cooldown_key(rung_cfg.position)):
                return rung_cfg.position

        # All cooled down — last rung is the final hope.
        return sorted(self._config.rungs, key=lambda r: r.position)[-1].position

    def _next_rung_position(self, current: int) -> int | None:
        for rung_cfg in sorted(self._config.rungs, key=lambda r: r.position):
            if rung_cfg.position > current:
                if not self.cooldowns.is_exhausted(self._cooldown_key(rung_cfg.position)):
                    return rung_cfg.position
        return None

    def _active_model_for_rung(self, position: int) -> str:
        for rung_cfg in self._config.rungs:
            if rung_cfg.position == position:
                if rung_cfg.adapter == "anthropic":
                    models = rung_cfg.models or {}
                    return models.get("default", "claude-sonnet-4-6")
                return rung_cfg.model or rung_cfg.adapter
        return "unknown"

    # ------------------------------------------------------------------
    # Pool selection (schema-3)
    # ------------------------------------------------------------------

    def _pool_candidates(
        self,
        task_class: str | None = None,
        mode: str = "hard",
    ) -> list[_CandidateInfo]:
        """Return the mode-ordered candidate list for health-aware selection.

        Ordering is a *preference*; health/cooldown always beat it downstream
        (dead or cooled candidates are skipped by _select_pool_candidate).

          hard     → rung-1 Sonnet, then pool frontier→strong→reliable
                     (coding-role bias when task_class == "coding"), floor last.
          light    → skip rung-1; pool reliable→strong→frontier with fast/general
                     roles preferred within each tier, floor last.
          research → rung-1 Sonnet (subprocess web search), then pool with
                     reasoning-role members first (tier order preserved), floor last.
          anything else → same as hard, no bias.
        """
        config = self._config
        assert config.pool is not None

        rung1_adapter = self._adapters.get(1)
        rung1: _CandidateInfo | None = None
        if rung1_adapter is not None:
            rung1 = _CandidateInfo(
                adapter=rung1_adapter,
                model=self._active_model_for_rung(1),
                quota_key=None,
                kind="rung1",
            )

        pool: list[_CandidateInfo] = []
        for member in config.pool.members_by_tier():
            pool_adapter = self._pool_adapters.get(member.model)
            if pool_adapter is not None:
                pool.append(_CandidateInfo(
                    adapter=pool_adapter,
                    model=member.model,
                    quota_key="openrouter_shared",
                    reasoning_mode=member.reasoning_mode,
                    roles=list(member.roles),
                    tier=member.tier,
                    kind="pool",
                ))

        floor: _CandidateInfo | None = None
        if config.floor is not None and self._floor_adapter is not None:
            floor = _CandidateInfo(
                adapter=self._floor_adapter,
                model=config.floor.model,
                quota_key=None,
                kind="floor",
            )

        if mode == "light":
            # Reversed tier order; fast/general-role members first within each tier.
            tier_rank: dict[str, int] = {
                t: i for i, t in enumerate(reversed(config.pool.tier_order))
            }
            pool = sorted(
                pool,
                key=lambda c: (
                    tier_rank.get(c.tier, 99),
                    0 if ({"fast", "general"} & set(c.roles)) else 1,
                ),
            )
            candidates = pool  # rung-1 deliberately excluded: preserve Pro quota
        elif mode == "research":
            pool = sorted(pool, key=lambda c: 0 if "reasoning" in c.roles else 1)
            candidates = ([rung1] if rung1 else []) + pool
        else:  # hard, or unresolved auto
            if mode == "hard" and task_class == "coding":
                pool = sorted(pool, key=lambda c: 0 if "coding" in c.roles else 1)
            candidates = ([rung1] if rung1 else []) + pool

        if floor is not None:
            candidates = candidates + [floor]

        for i, c in enumerate(candidates):
            c.idx = i
        return candidates

    def _select_pool_candidate(
        self,
        candidates: list[_CandidateInfo],
        tried: set[str],
    ) -> _CandidateInfo | None:
        """Return the first live candidate not already tried this turn.

        Skips: already tried, shared quota exhausted, per-model rate-limit, dead (backoff).
        """
        for c in candidates:
            if c.model in tried:
                continue
            if c.quota_key and self.cooldowns.is_exhausted(c.quota_key):
                continue
            if self.cooldowns.is_exhausted(c.model):
                continue
            if self.model_health.is_dead(c.model):
                continue
            return c
        return None

    # ------------------------------------------------------------------
    # Recovery probe (T05) — fires once on the dead→eligible promotion edge
    # ------------------------------------------------------------------

    def _needs_recovery_probe(self, candidate: _CandidateInfo) -> bool:
        """Return True only when a pool member is on the dead→eligible edge.

        Conditions (ALL must hold):
          - pool.recovery_probe is on
          - candidate is an OpenRouter pool member (quota_key set)
          - ModelHealth shows it has failed before (consecutive_failures > 0)
          - It is currently eligible (backoff window expired — is_dead is False)
        A model that has never failed is never probed.
        """
        if self._config.pool is None or not self._config.pool.recovery_probe:
            return False
        if candidate.quota_key is None:
            return False  # Sonnet or Ollama — never probe
        entry = self.model_health._data.get(candidate.model)
        if entry is None or entry.consecutive_failures == 0:
            return False  # never failed, or cleared by mark_success
        return not self.model_health.is_dead(candidate.model)

    def _is_probe_cached(self, model: str) -> bool:
        expiry = self._probe_cache.get(model)
        return expiry is not None and datetime.now(UTC) < expiry

    def _set_probe_cache(self, model: str) -> None:
        """Mark a probe as in-flight / recently completed for the storm-guard window."""
        self._probe_cache[model] = (
            datetime.now(UTC) + timedelta(seconds=_PROBE_CACHE_TTL_SECONDS)
        )

    async def _select_with_recovery_probe(
        self,
        candidates: list[_CandidateInfo],
        tried: set[str],
    ) -> _CandidateInfo | None:
        """Select the next live candidate, probing recovering models before promoting them.

        If a candidate is on the dead→eligible edge (has failed before, backoff just
        expired), fire one deep health_check to confirm the provider is actually back.
        On probe pass: mark_success and return the candidate.
        On probe fail: mark_unavailable (re-enter backoff), add to tried, and continue
        scanning for the next viable candidate.

        Cache the probe result for _PROBE_CACHE_TTL_SECONDS to prevent probe storms
        when two turns run near-simultaneously on the recovery edge.
        """
        while True:
            candidate = self._select_pool_candidate(candidates, tried)
            if candidate is None:
                return None

            if not self._needs_recovery_probe(candidate):
                return candidate  # hot path — live model, no probe needed

            if self._is_probe_cached(candidate.model):
                log.debug(
                    "recovery probe for %s: cache hit, skipping re-probe",
                    candidate.model,
                )
                return candidate

            # Set cache BEFORE the await so concurrent turns see it immediately
            self._set_probe_cache(candidate.model)
            log.info("recovery probe: checking %s after backoff expiry", candidate.model)

            probe_ok = False
            probe_detail = ""
            try:
                # quota_key guard above ensures this is always an OpenRouterAdapter;
                # deep=True is OR-specific — type: ignore silences the Protocol mismatch.
                status = await candidate.adapter.health_check(deep=True)  # type: ignore[call-arg]
                probe_ok = status.healthy
                probe_detail = status.detail
            except Exception as exc:
                probe_ok = False
                probe_detail = str(exc)
                log.warning("recovery probe for %s raised: %s", candidate.model, exc)

            if probe_ok:
                self.model_health.mark_success(candidate.model)
                log.info("recovery probe: %s confirmed healthy", candidate.model)
                return candidate

            mins = self.model_health.mark_unavailable(candidate.model, probe_detail)
            log.info(
                "recovery probe: %s still down, dead %dm more", candidate.model, mins
            )
            tried.add(candidate.model)
            # Loop: _select_pool_candidate will skip this model (in tried) next iteration

    # ------------------------------------------------------------------
    # Streaming with failover
    # ------------------------------------------------------------------

    async def complete_stream(
        self,
        request: NexusRequest,
        thread_yaml: dict[str, Any],
        live_history_filename: str,
    ) -> AsyncGenerator[CascadeEvent, None]:
        """Stream tokens with automatic failover.

        Classifies the turn once before routing so every response — regardless of
        which rung or pool member ultimately answers — gets a turn_classified event.
        Resolves the turn mode (auto → light/hard/research via the classifier) and
        rewrites request.mode to the resolved mode so adapters see the final value.
        Dispatches to the pool-aware path (schema-3) or the legacy fixed-rung path
        (schema-2) depending on config.uses_pool.
        """
        self._capture_loop_if_needed()
        requested_mode: str = request.mode

        # Classify the turn once, before any routing, on every path.
        task_class: str | None = None
        classification: ClassificationEvent | None = None
        if self._config.classifier.enabled:
            user_message = next(
                (m.content for m in reversed(request.messages) if m.role == "user"),
                "",
            )
            actual_backend = self._config.classifier.backend
            cls: Classification
            if actual_backend == "ollama" and self._floor_adapter is not None:
                try:
                    cls = await classify_ollama(user_message, self._floor_adapter)
                except Exception:
                    cls = classify_heuristic(user_message)
                    actual_backend = "heuristic"
            else:
                cls = classify_heuristic(user_message)
                actual_backend = "heuristic"
            task_class = cls.task_class
            classification = ClassificationEvent(
                task_class=cls.task_class,
                confidence=cls.confidence,
                reason=cls.reason,
                backend=actual_backend,
            )

        # Resolve auto → effective mode. Explicit modes pass through unchanged.
        # Unresolvable auto (classifier off) keeps the pre-3W Sonnet-first order.
        mode = requested_mode
        if mode == "auto":
            mode = _AUTO_MODE_MAP.get(task_class or "", "hard")
        if classification is not None:
            classification.mode = mode
            yield classification

        # Adapters see the resolved mode (rung-1 appends the research preamble on it).
        request = request.model_copy(update={"mode": mode})

        if self._config.uses_pool:
            async for ev in self._complete_stream_pool(
                request, thread_yaml, live_history_filename, task_class,
                mode=mode, requested_mode=requested_mode,
            ):
                yield ev
        else:
            rung_pos = self.select_rung(thread_yaml)
            yield MetaEvent(
                mode=mode,
                requested_mode=requested_mode,
                model=self._active_model_for_rung(rung_pos),
            )
            async for ev in self._complete_stream_legacy(
                request, thread_yaml, live_history_filename
            ):
                yield ev

    async def _complete_stream_legacy(
        self,
        request: NexusRequest,
        thread_yaml: dict[str, Any],
        live_history_filename: str,
    ) -> AsyncGenerator[CascadeEvent, None]:
        """Fixed-rung failover path (schema-2 configs). Unchanged from Phase 2b."""
        rung_pos = self.select_rung(thread_yaml)
        handover_context: str | None = None
        sequence = len(thread_yaml.get("handovers", []))

        while True:
            adapter = self._adapters.get(rung_pos)
            if not adapter:
                yield ErrorEvent(message=f"No adapter for rung {rung_pos}", terminal=True)
                return

            patched = self._inject_handover(request, handover_context)
            started = datetime.now(UTC)
            try:
                async for token in adapter.complete_stream(patched):
                    for ev in self._translate_token(token):
                        yield ev
                duration_ms = (datetime.now(UTC) - started).total_seconds() * 1000
                self.latency.record(adapter.name, duration_ms)
                if self.latency.is_degrading(adapter.name, duration_ms):
                    log.warning(
                        "Adapter %s is degrading; next call will prefer a lower rung.",
                        adapter.name,
                    )
                return

            except AdapterError as exc:
                outcome = classify_error(exc, adapter.name)
                log.info(
                    "Trigger %s on rung %d: %s",
                    outcome.trigger.value, rung_pos, outcome.detail,
                )

                if outcome.cooldown_seconds is not None:
                    self.cooldowns.mark_exhausted(
                        self._cooldown_key(rung_pos), outcome.cooldown_seconds
                    )

                if outcome.retryable:
                    try:
                        async for token in adapter.complete_stream(patched):
                            for ev in self._translate_token(token):
                                yield ev
                        return
                    except AdapterError as retry_exc:
                        outcome = classify_error(retry_exc, adapter.name)
                        if outcome.cooldown_seconds is not None:
                            self.cooldowns.mark_exhausted(
                                self._cooldown_key(rung_pos), outcome.cooldown_seconds
                            )

                if not outcome.failover:
                    yield ErrorEvent(message=outcome.detail, terminal=True)
                    return

                next_pos = self._next_rung_position(rung_pos)
                if next_pos is None:
                    yield ErrorEvent(
                        message="All rungs exhausted — no fallback available.",
                        terminal=True,
                    )
                    return

                sequence += 1
                from_model = self._active_model_for_rung(rung_pos)
                to_model = self._active_model_for_rung(next_pos)
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
                    to_rung=next_pos,
                    from_model=from_model,
                    to_model=to_model,
                    handover_path=handover_path,
                    trigger=outcome.trigger.value,
                )
                handover_context = self._read_handover_for_prompt(handover_path)
                rung_pos = next_pos

    async def _complete_stream_pool(
        self,
        request: NexusRequest,
        thread_yaml: dict[str, Any],
        live_history_filename: str,
        task_class: str | None = None,
        mode: str = "hard",
        requested_mode: str = "auto",
    ) -> AsyncGenerator[CascadeEvent, None]:
        """Health-aware pool streaming (schema-3 configs).

        Selection order comes from _pool_candidates(mode) — mode is a preference;
        health/cooldown always beat it (dead/cooled candidates are skipped).
        Records outcomes so the health ledger stays current without proactive probing.
        """
        all_candidates = self._pool_candidates(task_class, mode)
        if not all_candidates:
            yield ErrorEvent(message="No pool candidates configured.", terminal=True)
            return

        handover_context: str | None = None
        sequence = len(thread_yaml.get("handovers", []))
        tried: set[str] = set()

        # Initial selection — honor explicit pin, even if the model is marked dead.
        # Pin beats mode: search the full (hard-ordered) list so a Sonnet pin works
        # even in light mode, where rung-1 is excluded from the preference order.
        # Pinned models bypass the probe (user is explicitly overriding health state).
        pinned: str | None = thread_yaml.get("pinned_model")
        candidate: _CandidateInfo | None = None
        if pinned:
            pin_pool = all_candidates
            if not any(c.model == pinned for c in pin_pool):
                pin_pool = self._pool_candidates(task_class, "hard")
            candidate = next((c for c in pin_pool if c.model == pinned), None)
        if candidate is None:
            candidate = await self._select_with_recovery_probe(all_candidates, tried)

        if candidate is None:
            # Everything dead/cooled at turn start — degrade to floor unconditionally
            candidate = all_candidates[-1]
            log.warning(
                "cascade degraded: all pool members dead/cooled, "
                "falling through to floor %s",
                candidate.model,
            )

        yield MetaEvent(mode=mode, requested_mode=requested_mode, model=candidate.model)
        research_notice_emitted = False

        while True:
            adapter = candidate.adapter
            patched = self._inject_handover(request, handover_context)
            started = datetime.now(UTC)
            reason = "hard_fail"

            # Research mode without rung-1: say so up front, once.
            if mode == "research" and candidate.kind != "rung1" and not research_notice_emitted:
                yield TokenEvent(token=Token(text=RESEARCH_FALLBACK_NOTICE, is_final=False))
                research_notice_emitted = True

            try:
                async for token in adapter.complete_stream(patched):
                    for ev in self._translate_token(token):
                        yield ev
                # Successful stream — record health and return
                self.model_health.mark_success(candidate.model)
                duration_ms = (datetime.now(UTC) - started).total_seconds() * 1000
                self.latency.record(adapter.name, duration_ms)
                return

            except AdapterModelUnavailable as exc:
                mins = self.model_health.mark_unavailable(candidate.model, str(exc))
                log.info(
                    "model %s unavailable, dead %dm: %s", candidate.model, mins, exc
                )
                reason = "model_unavailable"

            except AdapterRateLimit as exc:
                # Per-model transient throttle — set cooldown but do NOT touch health counter
                self.cooldowns.mark_exhausted(candidate.model, exc.cooldown_seconds)
                log.info(
                    "model %s rate-limited %ds", candidate.model, exc.cooldown_seconds
                )
                reason = "rate_limited"

            except AdapterQuotaExhausted as exc:
                # Account-wide daily quota — marks shared key, cascades past all OR members
                key = exc.cooldown_key or "openrouter_shared"
                self.cooldowns.mark_exhausted(key, _QUOTA_COOLDOWN_SECONDS)
                log.info("OpenRouter daily quota exhausted (key=%s)", key)
                reason = "quota_exhausted"

            except AdapterError as exc:
                outcome = classify_error(exc, adapter.name)
                if outcome.cooldown_seconds is not None:
                    self.cooldowns.mark_exhausted(candidate.model, outcome.cooldown_seconds)
                reason = outcome.trigger.value.lower()
                log.info("adapter error on %s: %s", candidate.model, outcome.detail)

            # Find next viable candidate (probe-aware)
            tried.add(candidate.model)
            next_candidate = await self._select_with_recovery_probe(all_candidates, tried)

            if next_candidate is None:
                # No live candidates remain — fall through to floor unconditionally
                floor = all_candidates[-1]
                if floor.model in tried:
                    yield ErrorEvent(
                        message="All pool candidates exhausted — no fallback available.",
                        terminal=True,
                    )
                    return
                next_candidate = floor
                log.warning(
                    "All live pool members unavailable — "
                    "falling through to floor %s unconditionally",
                    floor.model,
                )

            # Generate handover doc and emit event
            sequence += 1
            trigger_outcome = TriggerOutcome(
                trigger=TriggerClass.HARD_FAIL,
                retryable=False,
                failover=True,
                detail=f"{candidate.model}: {reason}",
            )
            handover_path = await self._generate_handover_safe(
                thread_id=request.thread_id,
                sequence=sequence,
                from_model=candidate.model,
                to_model=next_candidate.model,
                trigger_outcome=trigger_outcome,
                transcript=request.messages,
                style_profile=request.style_profile,
                full_history_filename=live_history_filename,
            )
            yield HandoverEvent(
                from_rung=candidate.idx,
                to_rung=next_candidate.idx,
                from_model=candidate.model,
                to_model=next_candidate.model,
                handover_path=handover_path,
                trigger=reason,
                reason=reason,
            )
            handover_context = self._read_handover_for_prompt(handover_path)
            candidate = next_candidate

    @staticmethod
    def _translate_token(token: Token) -> list[CascadeEvent]:
        """Convert a Token to one or more CascadeEvents.

        Marker tokens (tool_call/tool_result set) become ToolCallEvent or
        ToolResultEvent and are NOT also yielded as TokenEvent, so they
        don't pollute the assistant content buffer.
        """
        if token.tool_call is not None:
            return [ToolCallEvent(
                name=token.tool_call.get("name", ""),
                input=dict(token.tool_call.get("input", {})),
            )]
        if token.tool_result is not None:
            return [ToolResultEvent(
                name=token.tool_result.get("name", ""),
                content=str(token.tool_result.get("content", "")),
            )]
        return [TokenEvent(token=token)]

    def _inject_handover(
        self, request: NexusRequest, handover_context: str | None
    ) -> NexusRequest:
        """Prepend handover brief to system prompt (immutably)."""
        if not handover_context:
            return request
        messages = list(request.messages)
        if messages and messages[0].role == "system":
            messages[0] = NexusMessage(
                role="system",
                content=handover_context + "\n\n" + messages[0].content,
            )
        else:
            messages.insert(0, NexusMessage(role="system", content=handover_context))
        return request.model_copy(update={"messages": messages})

    def _read_handover_for_prompt(self, path: Path) -> str:
        handover = load_handover(path)
        return format_handover_for_prompt(handover)

    # ------------------------------------------------------------------
    # Handover generation
    # ------------------------------------------------------------------

    async def _generate_handover_safe(
        self,
        *,
        thread_id: str,
        sequence: int,
        from_model: str,
        to_model: str,
        trigger_outcome: TriggerOutcome,
        transcript: list[NexusMessage],
        style_profile: StyleProfile,
        full_history_filename: str,
    ) -> Path:
        """Generate a handover doc; fall back to template if Haiku is unavailable."""
        if self._summariser is not None:
            try:
                return await generate_handover(
                    summariser=self._summariser,
                    vault_root=self.vault_root,
                    thread_id=thread_id,
                    sequence=sequence,
                    from_model=from_model,
                    to_model=to_model,
                    trigger_outcome=trigger_outcome,
                    transcript=transcript,
                    style_profile=style_profile,
                    full_history_filename=full_history_filename,
                    summariser_model_id="claude-haiku-4-5",
                )
            except AdapterError as e:
                log.warning("Haiku summariser failed (%s); using fallback template.", e)
        return self._fallback_handover(
            thread_id=thread_id,
            sequence=sequence,
            from_model=from_model,
            to_model=to_model,
            trigger_outcome=trigger_outcome,
            transcript=transcript,
            full_history_filename=full_history_filename,
        )

    def _fallback_handover(
        self,
        *,
        thread_id: str,
        sequence: int,
        from_model: str,
        to_model: str,
        trigger_outcome: TriggerOutcome,
        transcript: list[NexusMessage],
        full_history_filename: str,
    ) -> Path:
        """Synthesise a minimal handover doc without any model call."""
        handover_id = str(uuid.uuid7())
        id_short = thread_id.replace("-", "")[:8]
        triggered_at = datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")
        wiki_link = f"[[{full_history_filename.replace('.md', '')}]]"

        last_user = next(
            (m.content for m in reversed(transcript) if m.role == "user"),
            "(no user message in transcript)",
        )

        body = (
            "## Summary\n\n"
            "[Auto-generated fallback — Haiku summariser was unavailable.]\n\n"
            f"Failover from **{from_model}** to **{to_model}**.\n"
            f"Reason: {trigger_outcome.detail}\n\n"
            "## Open Questions\n\n"
            "- (Full summary unavailable — Haiku was unreachable during failover)\n\n"
            "## Last User Intent\n\n"
            f"{last_user[:500]}\n\n"
            "## Recommended Continuation Style\n\n"
            "Continue from the transcript. A full handover was not available.\n\n"
            "## Full History Path\n\n"
            f"{wiki_link}\n"
        )

        frontmatter: dict[str, Any] = {
            "type": "handover",
            "schema_version": 2,
            "handover_id": handover_id,
            "thread_id": thread_id,
            "sequence": sequence,
            "from_model": from_model,
            "to_model": to_model,
            "trigger": trigger_outcome.trigger.value,
            "trigger_detail": trigger_outcome.detail,
            "triggered_at": triggered_at,
            "generated_by": "orchestrator",
            "summariser_model": "fallback-template",
            "tokens_used": 0,
        }
        fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        markdown = f"---\n{fm_yaml}---\n\n{body}\n"

        handovers_dir = self.vault_root / "Orchestrator" / "Handovers"
        handovers_dir.mkdir(parents=True, exist_ok=True)
        out_path = handovers_dir / f"{id_short}_handover_{sequence:02d}.md"
        out_path.write_text(markdown, encoding="utf-8")
        return out_path

    # ------------------------------------------------------------------
    # Health and lifecycle
    # ------------------------------------------------------------------

    async def health_check_all(self) -> dict[int, Any]:
        self._capture_loop_if_needed()
        results: dict[int, Any] = {}
        for pos, adapter in self._adapters.items():
            results[pos] = await adapter.health_check()
        return results

    def pool_health_status(self) -> list[dict[str, Any]] | None:
        """Return ModelHealth status (+ tier) for all pool members, or None for legacy configs."""
        if not self._config.uses_pool or self._config.pool is None:
            return None
        members = self._config.pool.members
        statuses = self.model_health.all_status([m.model for m in members])
        for status, member in zip(statuses, members, strict=True):
            status["tier"] = member.tier
        return statuses

    def shutdown(self) -> None:
        self._watcher.stop()
