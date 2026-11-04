# P2c-T02 — Model Pool Schema

## Goal

Replace the fixed rungs 2-4 with a **pool** of free models carrying tier and role metadata. Rung 1 (Sonnet, subscription) and the Ollama floor stay as explicit rungs. Backward compatible: existing fixed-rung configs must still parse.

## Read first

- `nexus/orchestrator/types.py` (RungConfig, CascadeConfig)
- `nexus/orchestrator/config.py` (load_cascade)
- `nexus-build/phase-2c/00-OVERVIEW.md` (the pool table + tier order)

## New cascade.yaml shape

```yaml
schema_version: 3            # bumped from 2
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
    enable_web_tools: false
    models:
      default: claude-sonnet-4-6
      premium: claude-opus-4-7
      summariser: claude-haiku-4-5

pool:                        # NEW — replaces fixed rungs 2-4
  adapter: openrouter
  auth_mode: api_key_free_tier
  tier_order: [frontier, strong, reliable]
  members:
    - model: nvidia/nemotron-3-super-120b-a12b:free
      tier: frontier
      roles: [reasoning, coding]
      reasoning_mode: exclude        # adapter strips/limits thinking output
    - model: openai/gpt-oss-120b:free
      tier: frontier
      roles: [reasoning, coding, general]
    - model: deepseek/deepseek-v4-flash:free
      tier: frontier
      roles: [reasoning, coding]
    - model: qwen/qwen3-coder:free
      tier: frontier
      roles: [coding]
    - model: minimax/minimax-m2.5:free
      tier: strong
      roles: [coding, general]
    - model: qwen/qwen3-next-80b-a3b-instruct:free
      tier: strong
      roles: [general]
    - model: google/gemma-4-31b-it:free
      tier: reliable
      roles: [general]
    - model: openai/gpt-oss-20b:free
      tier: reliable
      roles: [general, fast]

floor:                       # NEW — the local sovereignty floor
  adapter: ollama
  auth_mode: local
  model: gemma4:latest
```

## Backward compatibility

`schema_version: 2` configs (fixed rungs 2-5, no `pool`/`floor`) must still load and behave exactly as before. Detect the shape: if `pool` is present, use pool routing (T04); else fall back to the legacy fixed-rung path. Keep both code paths until Phase 2c acceptance proves the pool path, then the legacy path can be deprecated in a later phase.

## Files to modify

```
nexus/orchestrator/types.py        ← PoolMember, PoolConfig, FloorConfig; CascadeConfig gains optional pool/floor
nexus/orchestrator/config.py       ← parse + validate the new shape
nexus/tests/test_types.py
nexus/tests/test_config.py (create if absent)
```

## `types.py`

```python
from typing import Literal

ModelTier = Literal["frontier", "strong", "reliable"]
ModelRole = Literal["reasoning", "coding", "general", "fast"]

class PoolMember(BaseModel):
    model: str
    tier: ModelTier
    roles: list[ModelRole] = Field(default_factory=lambda: ["general"])
    reasoning_mode: Literal["include", "exclude"] | None = None

class PoolConfig(BaseModel):
    adapter: AdapterName
    auth_mode: AuthMode
    tier_order: list[ModelTier] = Field(default_factory=lambda: ["frontier", "strong", "reliable"])
    members: list[PoolMember]

    def members_by_tier(self) -> list[PoolMember]:
        """Members sorted by tier_order, preserving in-tier declaration order."""
        rank = {t: i for i, t in enumerate(self.tier_order)}
        return sorted(self.members, key=lambda m: rank.get(m.tier, 99))

class FloorConfig(BaseModel):
    adapter: AdapterName
    auth_mode: AuthMode
    model: str

class CascadeConfig(BaseModel):
    schema_version: int
    rungs: list[RungConfig]
    pool: PoolConfig | None = None        # NEW
    floor: FloorConfig | None = None      # NEW
    jurisdictions: dict = Field(default_factory=dict)

    @property
    def uses_pool(self) -> bool:
        return self.pool is not None
```

## `config.py` validation

- If `pool` present, require at least one member and a non-empty `tier_order`.
- Every member's `tier` must appear in `tier_order` (else raise ConfigError naming the offending model).
- Duplicate model ids in the pool → ConfigError.
- If `pool` present but `floor` absent, warn (the cascade would have no local fallback) but don't fail.
- `schema_version: 3` with no `pool` is allowed (acts like legacy).

## Tests

`tests/test_types.py`:
- `PoolMember` defaults roles to `["general"]`.
- `members_by_tier` orders frontier → strong → reliable, stable within tier.
- `CascadeConfig.uses_pool` true when pool present, false otherwise.

`tests/test_config.py`:
- Loads the new schema-3 pool config successfully.
- Loads a legacy schema-2 fixed-rung config successfully (backward compat).
- Member with tier not in tier_order → ConfigError naming the model.
- Duplicate model id in pool → ConfigError.
- Pool present, floor absent → loads with a warning (capture via caplog).

## Definition of done

- [ ] `PoolMember`, `PoolConfig`, `FloorConfig` in types.py; `CascadeConfig` gains optional `pool`/`floor`.
- [ ] `config.py` parses + validates schema-3 and still parses schema-2.
- [ ] All tests pass.
- [ ] `~/nexus-test-vault/Orchestrator/Config/cascade.yaml` rewritten to schema-3 pool form (keep a backup of the schema-2 file first).
- [ ] Orchestrator still starts and `/health` responds (selection still uses legacy path until T04 — that's fine; this ticket only adds the data model).
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't change selection logic — T04 reads this schema.
- Don't add health tracking — T03.
- Don't delete the legacy fixed-rung code path.
