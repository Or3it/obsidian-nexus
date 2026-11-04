"""YAML configuration loader for cascade.yaml, tokenizer_ratios.yaml.

Hot-reload is not implemented in Phase 1. The function signatures are stable
so hot-reload can be layered on in Phase 4 without changing call sites.
# TODO(phase-4): add inotify/FSEvents watcher; on change call load_cascade()
#   and replace the running config if validation passes, else log and keep last good.
#   See OPEN_QUESTIONS.md entry added by P1-T02.
"""

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from .types import CascadeConfig, PoolConfig, ProjectsConfig, TokenizerRatios

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised on invalid or missing config."""


def load_cascade(path: Path) -> CascadeConfig:
    """Load and validate Config/cascade.yaml. Raise ConfigError on failure."""
    if not path.exists():
        raise ConfigError(f"cascade config not found at {path}")
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        cfg = CascadeConfig(**raw)
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        raise ConfigError(f"invalid cascade config at {path}: {e}") from e

    if cfg.pool is not None:
        _validate_pool(cfg.pool, cfg, path)

    return cfg


def _validate_pool(pool: PoolConfig, cfg: CascadeConfig, path: Path) -> None:
    """Validate pool-specific constraints that Pydantic alone cannot express."""
    if not pool.tier_order:
        raise ConfigError(f"pool.tier_order must not be empty (in {path})")

    if not pool.members:
        raise ConfigError(f"pool must have at least one member (in {path})")

    valid_tiers = set(pool.tier_order)
    seen: set[str] = set()
    for member in pool.members:
        if member.tier not in valid_tiers:
            raise ConfigError(
                f"pool member '{member.model}' has tier '{member.tier}' "
                f"not in tier_order {list(pool.tier_order)} (in {path})"
            )
        if member.model in seen:
            raise ConfigError(
                f"duplicate model id '{member.model}' in pool (in {path})"
            )
        seen.add(member.model)

    if cfg.floor is None:
        log.warning(
            "cascade config %s has a pool but no floor — no local fallback available", path
        )


def load_projects_config(path: Path) -> ProjectsConfig:
    """Read the ``projects:`` block from cascade.yaml; return defaults if absent."""
    if not path.exists():
        return ProjectsConfig()
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        projects_raw = raw.get("projects") if isinstance(raw, dict) else None
        if projects_raw and isinstance(projects_raw, dict):
            return ProjectsConfig(**projects_raw)
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        log.warning("Could not load projects config from %s: %s — using defaults", path, e)
    return ProjectsConfig()


def load_tokenizer_ratios(path: Path) -> TokenizerRatios:
    """Load Config/tokenizer_ratios.yaml. Return defaults if file is missing."""
    if not path.exists():
        return TokenizerRatios(ratios={})
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return TokenizerRatios(**raw)
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        raise ConfigError(f"invalid tokenizer_ratios config at {path}: {e}") from e
