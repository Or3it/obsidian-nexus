# P1-T02 — Core Types + Config Loading

## Goal

Define the Pydantic models that flow through the system, and implement YAML config loading with hot-reload watch (deferred — Phase 1 just loads at startup; the *interface* anticipates hot-reload).

## Read first

- `shared/adapter-protocol.md` (types definitions are canonical here)
- `shared/yaml-schemas.md`
- `CLAUDE.md` rule #7 (hot-reloadable YAML)

## Files to create or replace

```
orchestrator/types.py
orchestrator/config.py
tests/test_types.py
tests/test_config.py
tests/fixtures/cascade.yaml           ← valid example
tests/fixtures/cascade_invalid.yaml   ← schema-violating example
tests/fixtures/cascade_typo.yaml      ← typo-in-enum example
```

## `orchestrator/types.py`

Implement exactly the types defined in `shared/adapter-protocol.md`:

- `StyleProfile`
- `NexusMessage`
- `NexusRequest`
- `NexusResponse`
- `Token`
- `HealthStatus`

Plus these additional types used by config loading:

```python
from typing import Literal
from pydantic import BaseModel, Field

# Auth modes are an enum, not a free string.
# Bare-string auth_mode lets typos like "subscriptin" load silently and break at runtime.
AuthMode = Literal[
    "subscription",
    "api_key",
    "api_key_free_tier",
    "api_key_paid",
    "local",
]

AdapterName = Literal["anthropic", "google", "openrouter", "ollama", "gmi"]


class TokenizerRatios(BaseModel):
    """chars-per-token ratios used by adapters that can't measure tokens."""
    # Keyed by model id
    ratios: dict[str, float]
    default: float = 3.8


class RungConfig(BaseModel):
    position: int
    adapter: AdapterName
    auth_mode: AuthMode
    model: str | None = None
    # Anthropic-specific (position 1) extras:
    use_ruflo: bool = True
    ruflo_version: str | None = None
    models: dict[str, str] | None = None    # {default, premium, summariser}


class CascadeConfig(BaseModel):
    schema_version: int = 2
    rungs: list[RungConfig]
    jurisdictions: dict[str, str] = Field(default_factory=dict)
```

Phase 1 only uses position-1 (Anthropic) but the schema accepts the full list; we validate the whole thing.

## `orchestrator/config.py`

```python
from pathlib import Path
import yaml
from pydantic import ValidationError
from .types import CascadeConfig, TokenizerRatios

class ConfigError(Exception):
    """Raised on invalid or missing config."""

def load_cascade(path: Path) -> CascadeConfig:
    """Load and validate Config/cascade.yaml. Raise ConfigError on failure."""
    if not path.exists():
        raise ConfigError(f"cascade config not found at {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return CascadeConfig(**raw)
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        raise ConfigError(f"invalid cascade config at {path}: {e}") from e

def load_tokenizer_ratios(path: Path) -> TokenizerRatios:
    """Load Config/tokenizer_ratios.yaml. Return defaults if file missing."""
    if not path.exists():
        return TokenizerRatios(ratios={})
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return TokenizerRatios(**raw)
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        raise ConfigError(f"invalid tokenizer_ratios config at {path}: {e}") from e
```

Hot-reload is **not implemented in Phase 1**. Add a TODO comment and an `OPEN_QUESTIONS.md` entry pointing to a Phase 4 ticket. The function shape should be stable enough that hot-reload can be added without changing call sites.

## Default config templates

These should be created at orchestrator startup if missing — but for Phase 1, ship example versions in `tests/fixtures/` and document the format in `README.md`. The orchestrator-bridge ticket (P1-T06) handles auto-creation at first run.

**`tests/fixtures/cascade.yaml`** (Phase 1 minimum):

```yaml
schema_version: 2
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
    use_ruflo: true
    ruflo_version: "{verify-at-install}"
    models:
      default: claude-sonnet-4-6
      premium: claude-opus-4-7
      summariser: claude-haiku-4-5
jurisdictions: {}
```

**`tests/fixtures/cascade_invalid.yaml`**: an example with `schema_version: 99` or a missing `rungs` key, used to test that `ConfigError` is raised.

**`tests/fixtures/cascade_typo.yaml`**: an example with a misspelled enum value (e.g., `auth_mode: subscriptin`), used to test that `ConfigError` is raised on enum typos rather than silently accepting them.

```yaml
schema_version: 2
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscriptin     # typo — must be rejected
    use_ruflo: true
    models:
      default: claude-sonnet-4-6
jurisdictions: {}
```

## Tests

`tests/test_types.py`:

- Round-trip serialization of `NexusRequest` and `NexusResponse`.
- `StyleProfile` rejects unknown values for `format`, `verbosity`, `posture`.
- `NexusMessage` rejects unknown roles.
- `RungConfig` accepts the five valid `auth_mode` values (`subscription`, `api_key`, `api_key_free_tier`, `api_key_paid`, `local`).
- `RungConfig` rejects an invalid `auth_mode` (e.g., `"subscriptin"`) with `pydantic.ValidationError`.
- `RungConfig` rejects an invalid `adapter` value.

`tests/test_config.py`:

- `load_cascade("tests/fixtures/cascade.yaml")` returns a `CascadeConfig` with one Anthropic rung at position 1.
- `load_cascade("tests/fixtures/cascade_invalid.yaml")` raises `ConfigError`.
- `load_cascade("tests/fixtures/cascade_typo.yaml")` raises `ConfigError` (the typo path — proves the `Literal` is actually validating, not silently accepting strings).
- `load_cascade(non_existent_path)` raises `ConfigError`.
- `load_tokenizer_ratios(non_existent_path)` returns defaults without raising.

## Definition of done

- [ ] All listed files exist and are minimally type-annotated.
- [ ] `pytest tests/test_types.py tests/test_config.py` passes.
- [ ] `ruff check orchestrator/types.py orchestrator/config.py` clean.
- [ ] `mypy orchestrator/types.py orchestrator/config.py` clean.
- [ ] The typo test (`cascade_typo.yaml`) explicitly fails on `auth_mode: subscriptin` — this is the proof that the `Literal` is enforced.

## Definition of NOT done

- Don't implement hot-reload yet. Leave the TODO comment.
- Don't create `Config/` directory in the vault yet (P1-T06 handles that).
- Don't import or call any adapter code — `types.py` and `config.py` should be importable on their own.
