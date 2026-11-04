"""Core domain types: protocol models and config schemas."""

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Adapter protocol types  (canonical source: shared/adapter-protocol.md)
# ---------------------------------------------------------------------------


class StyleProfile(BaseModel):
    format: Literal["prose", "structured", "mixed"] = "prose"
    verbosity: Literal["concise", "default", "detailed"] = "default"
    posture: Literal["assistant", "peer", "tutor"] = "assistant"


class NexusMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


# Turn routing mode (Phase 3W). "auto" resolves via the task classifier;
# the engine rewrites request.mode to the resolved mode before adapter calls,
# so adapters only ever see light/hard/research (or auto when unresolvable).
TurnMode = Literal["auto", "light", "hard", "research"]


class NexusRequest(BaseModel):
    thread_id: str
    messages: list[NexusMessage]
    style_profile: StyleProfile
    selection_hint: str | None = None
    max_output_tokens: int | None = None
    stream: bool = True
    mode: TurnMode = "auto"


class NexusResponse(BaseModel):
    content: str
    model_id: str
    auth_mode: Literal["subscription", "api_key_free_tier", "api_key_paid", "local"]
    estimated_tokens: int
    duration_ms: int
    finish_reason: Literal["stop", "length", "refusal", "error"]
    raw_metadata: dict = Field(default_factory=dict)  # type: ignore[type-arg]


class Token(BaseModel):
    text: str
    is_final: bool = False
    # Optional fields used by the SDK tool-call path to carry tool events through
    # the AsyncIterator[Token] interface without breaking the Adapter protocol.
    # The cascade engine translates these to ToolCallEvent / ToolResultEvent.
    tool_call: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None


class HealthStatus(BaseModel):
    healthy: bool
    last_checked: datetime
    detail: str = ""


# ---------------------------------------------------------------------------
# Config types
# ---------------------------------------------------------------------------

# Five valid auth modes. Using Literal rather than a free string catches
# typos (e.g. "subscriptin") at config-load time instead of failing at runtime.
AuthMode = Literal[
    "subscription",
    "api_key",
    "api_key_free_tier",
    "api_key_paid",
    "local",
]

AdapterName = Literal["anthropic", "google", "openrouter", "ollama", "gmi"]


class TokenizerRatios(BaseModel):
    """chars-per-token ratios used by adapters that can't measure tokens directly."""

    ratios: dict[str, float]
    default: float = 3.8


class OpusHeuristicConfig(BaseModel):
    """Config for the Opus auto-escalation heuristic (cascade.yaml: opus_heuristic block)."""

    enabled: bool = False
    min_chars: int = 500
    keywords: list[str] | None = None  # None → use module defaults


class RungConfig(BaseModel):
    position: int
    adapter: AdapterName
    auth_mode: AuthMode
    model: str | None = None
    # Anthropic-specific extras (position 1):
    use_ruflo: bool = True
    ruflo_version: str | None = None
    models: dict[str, str] | None = None  # {default, premium, summariser}
    enable_web_tools: bool = False  # SDK path with web_search tool; requires ANTHROPIC_API_KEY
    opus_heuristic: OpusHeuristicConfig | None = None  # auto-escalation; disabled when absent


# ---------------------------------------------------------------------------
# Pool types (Phase 2c — health-aware pool routing)
# ---------------------------------------------------------------------------

ModelTier = Literal["frontier", "strong", "reliable"]
ModelRole = Literal["reasoning", "coding", "general", "fast"]


class PoolMember(BaseModel):
    """One free model in the routing pool, annotated with tier and roles."""

    model: str
    tier: ModelTier
    roles: list[ModelRole] = Field(default_factory=lambda: ["general"])  # type: ignore[arg-type]
    reasoning_mode: Literal["include", "exclude"] | None = None


class PoolConfig(BaseModel):
    """Pool of free OpenRouter models replacing fixed rungs 2-4."""

    adapter: AdapterName
    auth_mode: AuthMode
    tier_order: list[ModelTier] = Field(
        default_factory=lambda: ["frontier", "strong", "reliable"]  # type: ignore[arg-type]
    )
    members: list[PoolMember]
    recovery_probe: bool = True  # fire a 1-token probe when a dead model's backoff expires

    def members_by_tier(self) -> list[PoolMember]:
        """Members sorted by tier_order, stable within each tier."""
        rank = {t: i for i, t in enumerate(self.tier_order)}
        return sorted(self.members, key=lambda m: rank.get(m.tier, 99))


class FloorConfig(BaseModel):
    """Local sovereignty floor (Ollama) — last resort before complete failure."""

    adapter: AdapterName
    auth_mode: AuthMode
    model: str


class ClassifierConfig(BaseModel):
    """Task classifier configuration (cascade.yaml: classifier block).

    Classification feeds auto-mode routing (Phase 3W): conversational/general →
    light, coding → hard, research → research. Explicit modes bypass it.
    """

    enabled: bool = True
    backend: Literal["heuristic", "ollama"] = "heuristic"
    # Deprecated (Phase 3W): superseded by mode-based routing. Parsed for
    # backward compatibility, ignored by selection, warns at config load.
    route_by_role: bool = False


class ProjectsConfig(BaseModel):
    """Discovery configuration — projects: block in cascade.yaml."""

    scan_root: Path = Field(default_factory=lambda: Path.home() / "Documents")
    ignore: list[str] = Field(default_factory=list)


class CascadeConfig(BaseModel):
    schema_version: int = 2
    rungs: list[RungConfig]
    pool: PoolConfig | None = None      # present → schema-3 pool routing
    floor: FloorConfig | None = None    # local fallback for pool configs
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    jurisdictions: dict[str, str] = Field(default_factory=dict)
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)

    @property
    def uses_pool(self) -> bool:
        """True when a pool block is configured (schema-3+); False for legacy fixed rungs."""
        return self.pool is not None
