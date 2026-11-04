"""Tests for cascade/handover.py — Handover document generation."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.cascade.handover import (
    format_handover_for_prompt,
    generate_handover,
    load_handover,
)
from orchestrator.cascade.summariser import Summariser
from orchestrator.cascade.triggers import TriggerClass, TriggerOutcome
from orchestrator.types import NexusMessage, StyleProfile

_BODY = (
    "## Summary\nTest summary.\n\n"
    "## Open Questions\n- None.\n\n"
    "## Last User Intent\nUser asked for X.\n\n"
    "## Recommended Continuation Style\nBe concise.\n\n"
    "## Full History Path\n[[test-thread]]"
)

_TRIGGER = TriggerOutcome(
    trigger=TriggerClass.HARD_FAIL,
    retryable=False,
    failover=True,
    detail="adapter unavailable",
)

_TRANSCRIPT = [
    NexusMessage(role="user", content="Hello"),
    NexusMessage(role="assistant", content="Hi there"),
]

_THREAD_ID = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"


def _make_summariser(body: str = _BODY) -> Summariser:
    adapter = MagicMock()
    s = Summariser(adapter)
    s.summarise_for_handover = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return s


async def _gen(
    tmp_path: Path,
    *,
    summariser: Summariser | None = None,
    thread_id: str = _THREAD_ID,
    sequence: int = 1,
    from_model: str = "claude-sonnet-4-6",
    to_model: str = "minimax/minimax-m2.5:free",
    trigger: TriggerOutcome = _TRIGGER,
    full_history_filename: str = "2026-05-16_thread-a1b2c3d4_test.md",
    summariser_model_id: str = "claude-haiku-4-5",
    body: str = _BODY,
) -> Path:
    s = summariser if summariser is not None else _make_summariser(body)
    return await generate_handover(
        summariser=s,
        vault_root=tmp_path,
        thread_id=thread_id,
        sequence=sequence,
        from_model=from_model,
        to_model=to_model,
        trigger_outcome=trigger,
        transcript=_TRANSCRIPT,
        style_profile=StyleProfile(),
        full_history_filename=full_history_filename,
        summariser_model_id=summariser_model_id,
    )


# ---------------------------------------------------------------------------
# generate_handover
# ---------------------------------------------------------------------------


class TestGenerateHandover:
    async def test_file_is_created(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path)
        assert path.exists()

    async def test_filename_uses_thread_id_short8(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path, thread_id=_THREAD_ID, sequence=1)
        assert path.name == "a1b2c3d4_handover_01.md"

    async def test_sequence_zero_padding_02(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path, sequence=2)
        assert path.name.endswith("_handover_02.md")

    async def test_sequence_zero_padding_09(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path, sequence=9)
        assert path.name.endswith("_handover_09.md")

    async def test_sequence_99(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path, sequence=99)
        assert path.name.endswith("_handover_99.md")

    async def test_handovers_dir_created(self, tmp_path: Path) -> None:
        await _gen(tmp_path)
        assert (tmp_path / "Orchestrator" / "Handovers").is_dir()

    async def test_body_matches_summariser_output(self, tmp_path: Path) -> None:
        custom_body = "## Summary\nCustom content only."
        path = await _gen(tmp_path, body=custom_body)
        result = load_handover(path)
        assert result["body"] == custom_body

    async def test_wiki_link_strips_md_extension(self, tmp_path: Path) -> None:
        s = _make_summariser()
        await _gen(
            tmp_path, summariser=s,
            full_history_filename="2026-05-16_thread-a1b2c3d4_test.md",
        )
        call_kwargs = s.summarise_for_handover.call_args.kwargs  # type: ignore[attr-defined]
        assert call_kwargs["full_history_wiki_link"] == "[[2026-05-16_thread-a1b2c3d4_test]]"

    async def test_wiki_link_no_double_stripping(self, tmp_path: Path) -> None:
        s = _make_summariser()
        await _gen(tmp_path, summariser=s, full_history_filename="noextension")
        call_kwargs = s.summarise_for_handover.call_args.kwargs  # type: ignore[attr-defined]
        assert call_kwargs["full_history_wiki_link"] == "[[noextension]]"


# ---------------------------------------------------------------------------
# generate_handover — frontmatter schema
# ---------------------------------------------------------------------------


class TestFrontmatterSchema:
    async def test_required_fields_present(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path)
        fm = load_handover(path)["frontmatter"]
        for field in (
            "type", "schema_version", "handover_id", "thread_id",
            "sequence", "from_model", "to_model", "trigger",
            "trigger_detail", "triggered_at", "generated_by",
            "summariser_model", "tokens_used",
        ):
            assert field in fm, f"Missing field: {field}"

    async def test_type_is_handover(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path)
        assert load_handover(path)["frontmatter"]["type"] == "handover"

    async def test_schema_version_is_2(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path)
        assert load_handover(path)["frontmatter"]["schema_version"] == 2

    async def test_trigger_value(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path)
        assert load_handover(path)["frontmatter"]["trigger"] == "HARD_FAIL"

    async def test_from_and_to_model(self, tmp_path: Path) -> None:
        path = await _gen(
            tmp_path, from_model="claude-sonnet-4-6",
            to_model="minimax/minimax-m2.5:free",
        )
        fm = load_handover(path)["frontmatter"]
        assert fm["from_model"] == "claude-sonnet-4-6"
        assert fm["to_model"] == "minimax/minimax-m2.5:free"

    async def test_generated_by_orchestrator(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path)
        assert load_handover(path)["frontmatter"]["generated_by"] == "orchestrator"

    async def test_thread_id_recorded(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path, thread_id=_THREAD_ID)
        assert load_handover(path)["frontmatter"]["thread_id"] == _THREAD_ID


# ---------------------------------------------------------------------------
# load_handover
# ---------------------------------------------------------------------------


class TestLoadHandover:
    async def test_round_trip_frontmatter(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path, sequence=5)
        fm = load_handover(path)["frontmatter"]
        assert fm["sequence"] == 5
        assert fm["thread_id"] == _THREAD_ID

    async def test_round_trip_body(self, tmp_path: Path) -> None:
        path = await _gen(tmp_path, body=_BODY)
        assert load_handover(path)["body"] == _BODY

    def test_missing_frontmatter_raises_value_error(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("No frontmatter here\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            load_handover(bad_file)

    def test_empty_file_raises_value_error(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.md"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            load_handover(empty)


# ---------------------------------------------------------------------------
# format_handover_for_prompt
# ---------------------------------------------------------------------------


def _sample_handover(trigger: str = "HARD_FAIL", body: str = _BODY) -> dict[str, Any]:
    return {
        "frontmatter": {
            "from_model": "claude-sonnet-4-6",
            "to_model": "minimax/minimax-m2.5:free",
            "trigger": trigger,
            "trigger_detail": "adapter unavailable",
        },
        "body": body,
    }


class TestFormatHandoverForPrompt:
    def test_non_empty_string(self) -> None:
        result = format_handover_for_prompt(_sample_handover())
        assert result

    def test_contains_trigger_class(self) -> None:
        result = format_handover_for_prompt(_sample_handover(trigger="OPENROUTER_QUOTA"))
        assert "OPENROUTER_QUOTA" in result

    def test_contains_body_content(self) -> None:
        result = format_handover_for_prompt(_sample_handover(body="Unique body content."))
        assert "Unique body content." in result

    def test_contains_from_and_to_model(self) -> None:
        result = format_handover_for_prompt(_sample_handover())
        assert "claude-sonnet-4-6" in result
        assert "minimax/minimax-m2.5:free" in result

    def test_trigger_detail_included(self) -> None:
        result = format_handover_for_prompt(_sample_handover())
        assert "adapter unavailable" in result
