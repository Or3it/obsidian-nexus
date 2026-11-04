"""Tests for orchestrator/types.py."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orchestrator.types import (
    AuthMode,
    CascadeConfig,
    FloorConfig,
    HealthStatus,
    NexusMessage,
    NexusRequest,
    NexusResponse,
    PoolConfig,
    PoolMember,
    RungConfig,
    StyleProfile,
    Token,
)

# ---------------------------------------------------------------------------
# StyleProfile
# ---------------------------------------------------------------------------


def test_style_profile_defaults() -> None:
    sp = StyleProfile()
    assert sp.format == "prose"
    assert sp.verbosity == "default"
    assert sp.posture == "assistant"


def test_style_profile_rejects_unknown_format() -> None:
    with pytest.raises(ValidationError):
        StyleProfile(format="bullet")  # type: ignore[arg-type]


def test_style_profile_rejects_unknown_verbosity() -> None:
    with pytest.raises(ValidationError):
        StyleProfile(verbosity="verbose")  # type: ignore[arg-type]


def test_style_profile_rejects_unknown_posture() -> None:
    with pytest.raises(ValidationError):
        StyleProfile(posture="adversarial")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# NexusMessage
# ---------------------------------------------------------------------------


def test_nexus_message_valid_roles() -> None:
    for role in ("system", "user", "assistant"):
        msg = NexusMessage(role=role, content="hello")  # type: ignore[arg-type]
        assert msg.role == role


def test_nexus_message_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        NexusMessage(role="function", content="oops")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# NexusRequest round-trip
# ---------------------------------------------------------------------------


def test_nexus_request_round_trip() -> None:
    req = NexusRequest(
        thread_id="abc-123",
        messages=[NexusMessage(role="user", content="Hello")],
        style_profile=StyleProfile(),
    )
    data = req.model_dump()
    restored = NexusRequest(**data)
    assert restored.thread_id == req.thread_id
    assert restored.messages[0].content == "Hello"
    assert restored.stream is True


# ---------------------------------------------------------------------------
# NexusResponse round-trip
# ---------------------------------------------------------------------------


def test_nexus_response_round_trip() -> None:
    resp = NexusResponse(
        content="Sure, here is the answer.",
        model_id="claude-sonnet-4-6",
        auth_mode="subscription",
        estimated_tokens=42,
        duration_ms=1500,
        finish_reason="stop",
    )
    data = resp.model_dump()
    restored = NexusResponse(**data)
    assert restored.content == resp.content
    assert restored.finish_reason == "stop"
    assert restored.raw_metadata == {}


def test_nexus_response_rejects_unknown_auth_mode() -> None:
    with pytest.raises(ValidationError):
        NexusResponse(
            content="x",
            model_id="m",
            auth_mode="api_key",  # type: ignore[arg-type]  # not in NexusResponse's Literal
            estimated_tokens=1,
            duration_ms=1,
            finish_reason="stop",
        )


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def test_token_defaults() -> None:
    t = Token(text="hello")
    assert t.is_final is False


# ---------------------------------------------------------------------------
# HealthStatus
# ---------------------------------------------------------------------------


def test_health_status() -> None:
    hs = HealthStatus(healthy=True, last_checked=datetime.now(tz=UTC))
    assert hs.detail == ""


# ---------------------------------------------------------------------------
# RungConfig — AuthMode validation
# ---------------------------------------------------------------------------


VALID_AUTH_MODES: list[AuthMode] = [
    "subscription",
    "api_key",
    "api_key_free_tier",
    "api_key_paid",
    "local",
]


@pytest.mark.parametrize("mode", VALID_AUTH_MODES)
def test_rung_config_accepts_valid_auth_mode(mode: AuthMode) -> None:
    rung = RungConfig(position=1, adapter="anthropic", auth_mode=mode)
    assert rung.auth_mode == mode


def test_rung_config_rejects_invalid_auth_mode() -> None:
    with pytest.raises(ValidationError):
        RungConfig(position=1, adapter="anthropic", auth_mode="subscriptin")  # type: ignore[arg-type]


def test_rung_config_rejects_invalid_adapter() -> None:
    with pytest.raises(ValidationError):
        RungConfig(position=1, adapter="openai", auth_mode="api_key")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PoolMember
# ---------------------------------------------------------------------------


def test_pool_member_defaults_roles_to_general() -> None:
    m = PoolMember(model="test/model:free", tier="frontier")
    assert m.roles == ["general"]


def test_pool_member_reasoning_mode_defaults_to_none() -> None:
    m = PoolMember(model="test/model:free", tier="strong")
    assert m.reasoning_mode is None


def test_pool_member_accepts_reasoning_mode_exclude() -> None:
    m = PoolMember(model="test/model:free", tier="frontier", reasoning_mode="exclude")
    assert m.reasoning_mode == "exclude"


def test_pool_member_rejects_invalid_tier() -> None:
    with pytest.raises(ValidationError):
        PoolMember(model="test/model:free", tier="ultra")  # type: ignore[arg-type]


def test_pool_member_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        PoolMember(model="test/model:free", tier="frontier", roles=["search"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# PoolConfig.members_by_tier
# ---------------------------------------------------------------------------


def _make_pool(*members: PoolMember) -> PoolConfig:
    return PoolConfig(
        adapter="openrouter",
        auth_mode="api_key_free_tier",
        tier_order=["frontier", "strong", "reliable"],
        members=list(members),
    )


def test_members_by_tier_orders_frontier_then_strong_then_reliable() -> None:
    a = PoolMember(model="a:free", tier="reliable", roles=["general"])
    b = PoolMember(model="b:free", tier="frontier", roles=["coding"])
    c = PoolMember(model="c:free", tier="strong", roles=["general"])
    pool = _make_pool(a, b, c)
    ordered = pool.members_by_tier()
    assert [m.model for m in ordered] == ["b:free", "c:free", "a:free"]


def test_members_by_tier_stable_within_tier() -> None:
    """Declaration order is preserved for members with the same tier."""
    x = PoolMember(model="x:free", tier="frontier", roles=["general"])
    y = PoolMember(model="y:free", tier="frontier", roles=["coding"])
    z = PoolMember(model="z:free", tier="frontier", roles=["general"])
    pool = _make_pool(x, y, z)
    ordered = pool.members_by_tier()
    assert [m.model for m in ordered] == ["x:free", "y:free", "z:free"]


# ---------------------------------------------------------------------------
# CascadeConfig.uses_pool
# ---------------------------------------------------------------------------


def _minimal_rung() -> RungConfig:
    return RungConfig(position=1, adapter="anthropic", auth_mode="subscription")


def test_cascade_config_uses_pool_false_when_no_pool() -> None:
    cfg = CascadeConfig(schema_version=2, rungs=[_minimal_rung()])
    assert cfg.uses_pool is False


def test_cascade_config_uses_pool_true_when_pool_present() -> None:
    pool = PoolConfig(
        adapter="openrouter",
        auth_mode="api_key_free_tier",
        members=[PoolMember(model="a:free", tier="frontier")],
    )
    cfg = CascadeConfig(schema_version=3, rungs=[_minimal_rung()], pool=pool)
    assert cfg.uses_pool is True


def test_cascade_config_floor_is_optional() -> None:
    """CascadeConfig with pool but no floor is a valid model (config.py warns, not errors)."""
    pool = PoolConfig(
        adapter="openrouter",
        auth_mode="api_key_free_tier",
        members=[PoolMember(model="a:free", tier="frontier")],
    )
    cfg = CascadeConfig(schema_version=3, rungs=[_minimal_rung()], pool=pool, floor=None)
    assert cfg.floor is None
    assert cfg.uses_pool is True


def test_floor_config_fields() -> None:
    floor = FloorConfig(adapter="ollama", auth_mode="local", model="gemma4:latest")
    assert floor.adapter == "ollama"
    assert floor.model == "gemma4:latest"
