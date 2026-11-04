"""Tests for orchestrator/config.py."""

import logging
from pathlib import Path

import pytest

from orchestrator.config import ConfigError, load_cascade, load_tokenizer_ratios


def test_load_cascade_valid(fixtures_dir: Path) -> None:
    cfg = load_cascade(fixtures_dir / "cascade.yaml")
    assert cfg.schema_version == 2
    assert len(cfg.rungs) == 1
    rung = cfg.rungs[0]
    assert rung.position == 1
    assert rung.adapter == "anthropic"
    assert rung.auth_mode == "subscription"
    assert rung.models is not None
    assert rung.models["default"] == "claude-sonnet-4-6"


def test_load_cascade_invalid_raises(fixtures_dir: Path) -> None:
    with pytest.raises(ConfigError):
        load_cascade(fixtures_dir / "cascade_invalid.yaml")


def test_load_cascade_typo_raises(fixtures_dir: Path) -> None:
    """cascade_typo.yaml has auth_mode: subscriptin — Literal must reject it."""
    with pytest.raises(ConfigError):
        load_cascade(fixtures_dir / "cascade_typo.yaml")


def test_load_cascade_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_cascade(tmp_path / "nonexistent.yaml")


def test_load_tokenizer_ratios_missing_returns_defaults(tmp_path: Path) -> None:
    ratios = load_tokenizer_ratios(tmp_path / "nonexistent.yaml")
    assert ratios.default == 3.8
    assert ratios.ratios == {}


def test_load_tokenizer_ratios_valid(tmp_path: Path) -> None:
    p = tmp_path / "tokenizer_ratios.yaml"
    p.write_text("ratios:\n  claude-sonnet-4-6: 3.6\ndefault: 3.8\n", encoding="utf-8")
    ratios = load_tokenizer_ratios(p)
    assert ratios.ratios["claude-sonnet-4-6"] == pytest.approx(3.6)
    assert ratios.default == pytest.approx(3.8)


# ---------------------------------------------------------------------------
# Schema-3 pool configs (P2c-T02)
# ---------------------------------------------------------------------------


def test_load_cascade_schema3_pool(fixtures_dir: Path) -> None:
    """Schema-3 pool config loads cleanly; uses_pool is True."""
    cfg = load_cascade(fixtures_dir / "cascade_pool.yaml")
    assert cfg.schema_version == 3
    assert cfg.uses_pool is True
    assert cfg.pool is not None
    assert cfg.floor is not None
    assert cfg.floor.model == "gemma4:latest"
    # Rung 1 still present
    assert len(cfg.rungs) == 1
    assert cfg.rungs[0].adapter == "anthropic"
    # Pool has expected members
    assert len(cfg.pool.members) == 5
    slugs = [m.model for m in cfg.pool.members]
    assert "openai/gpt-oss-120b:free" in slugs
    assert "google/gemma-4-31b-it:free" in slugs


def test_load_cascade_schema3_members_by_tier(fixtures_dir: Path) -> None:
    """members_by_tier returns frontier before strong before reliable."""
    cfg = load_cascade(fixtures_dir / "cascade_pool.yaml")
    assert cfg.pool is not None
    ordered = cfg.pool.members_by_tier()
    tiers = [m.tier for m in ordered]
    # All frontier members precede strong, which precede reliable
    frontier_indices = [i for i, t in enumerate(tiers) if t == "frontier"]
    strong_indices = [i for i, t in enumerate(tiers) if t == "strong"]
    reliable_indices = [i for i, t in enumerate(tiers) if t == "reliable"]
    assert max(frontier_indices) < min(strong_indices)
    assert max(strong_indices) < min(reliable_indices)


def test_load_cascade_schema2_full_backward_compat(fixtures_dir: Path) -> None:
    """Schema-2 multi-rung fixed config still loads without pool/floor."""
    cfg = load_cascade(fixtures_dir / "cascade_schema2_full.yaml")
    assert cfg.schema_version == 2
    assert cfg.uses_pool is False
    assert cfg.pool is None
    assert cfg.floor is None
    assert len(cfg.rungs) == 5
    positions = [r.position for r in cfg.rungs]
    assert positions == [1, 2, 3, 4, 5]


def test_load_cascade_pool_member_tier_not_in_tier_order(tmp_path: Path) -> None:
    """Member tier absent from tier_order → ConfigError naming the model."""
    p = tmp_path / "bad_tier.yaml"
    p.write_text(
        """\
schema_version: 3
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
pool:
  adapter: openrouter
  auth_mode: api_key_free_tier
  tier_order: [frontier, strong]
  members:
    - model: test/model:free
      tier: reliable
floor:
  adapter: ollama
  auth_mode: local
  model: gemma4:latest
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="test/model:free"):
        load_cascade(p)


def test_load_cascade_pool_duplicate_model(tmp_path: Path) -> None:
    """Duplicate model id in pool → ConfigError naming the duplicate."""
    p = tmp_path / "dup.yaml"
    p.write_text(
        """\
schema_version: 3
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
pool:
  adapter: openrouter
  auth_mode: api_key_free_tier
  tier_order: [frontier]
  members:
    - model: test/model:free
      tier: frontier
    - model: test/model:free
      tier: frontier
floor:
  adapter: ollama
  auth_mode: local
  model: gemma4:latest
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="test/model:free"):
        load_cascade(p)


def test_load_cascade_pool_no_floor_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Pool present but floor absent → loads successfully, emits a warning."""
    p = tmp_path / "no_floor.yaml"
    p.write_text(
        """\
schema_version: 3
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
pool:
  adapter: openrouter
  auth_mode: api_key_free_tier
  tier_order: [frontier]
  members:
    - model: test/model:free
      tier: frontier
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="orchestrator.config"):
        cfg = load_cascade(p)
    assert cfg.uses_pool is True
    assert cfg.floor is None
    assert any("no local fallback" in r.message for r in caplog.records)
