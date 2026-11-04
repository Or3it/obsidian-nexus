"""Tests for orchestrator.projects.summarizer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.projects.summarizer import (
    _build_prompt,
    _parse,
    _read_readme,
    summarize,
)
from orchestrator.projects.types import ProjectCandidate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _candidate(
    name: str = "My Project",
    slug: str = "my-project",
    languages: list[str] | None = None,
    readme_path: str | None = None,
    file_tree: list[str] | None = None,
) -> ProjectCandidate:
    return ProjectCandidate(
        name=name,
        slug=slug,
        path=f"/tmp/{slug}",
        last_modified="2026-06-01T00:00:00+00:00",
        languages=languages or ["python"],
        has_readme=readme_path is not None,
        readme_path=readme_path,
        file_tree=file_tree or ["README.md", "main.py"],
        git=None,
        markers=["pyproject.toml"],
        inferred_status="active",
    )


def _json_response(
    summary: str = "A great project.",
    stack: list[str] | None = None,
    category: str = "other",
) -> str:
    return json.dumps(
        {
            "summary": summary,
            "stack": stack or ["python"],
            "category": category,
        }
    )


# ---------------------------------------------------------------------------
# _parse — JSON parsing and fallback
# ---------------------------------------------------------------------------


def test_parse_valid_json() -> None:
    raw = _json_response("Does X.", ["python", "flask"], "trading")
    result = _parse(raw, _candidate())
    assert result.summary == "Does X."
    assert result.stack == ["python", "flask"]
    assert result.category == "trading"


def test_parse_json_in_markdown_fences() -> None:
    raw = "```json\n" + _json_response("Does Y.", ["typescript"]) + "\n```"
    result = _parse(raw, _candidate())
    assert result.summary == "Does Y."
    assert result.stack == ["typescript"]


def test_parse_json_in_plain_fences() -> None:
    raw = "```\n" + _json_response("Does Z.", ["go"]) + "\n```"
    result = _parse(raw, _candidate())
    assert result.summary == "Does Z."
    assert result.stack == ["go"]


def test_parse_garbage_falls_back_to_minimal_summary() -> None:
    raw = "I cannot do that. Here is prose instead."
    c = _candidate(name="Sample Scraper", languages=["typescript"])
    result = _parse(raw, c)
    assert "Sample Scraper" in result.summary
    assert "typescript" in result.summary
    assert result.stack == ["typescript"]
    assert result.category == "other"


def test_parse_empty_response_falls_back() -> None:
    result = _parse("", _candidate(name="EmptyProj", languages=["rust"]))
    assert "EmptyProj" in result.summary
    assert result.category == "other"


def test_parse_unknown_category_defaults_to_other() -> None:
    raw = _json_response("Does stuff.", category="blockchain")
    result = _parse(raw, _candidate())
    assert result.category == "other"


def test_parse_missing_stack_defaults_to_empty() -> None:
    raw = json.dumps({"summary": "Nice project.", "category": "worship"})
    result = _parse(raw, _candidate(languages=[]))
    assert result.stack == []
    assert result.summary == "Nice project."
    assert result.category == "worship"


def test_parse_empty_summary_field_triggers_fallback() -> None:
    raw = json.dumps({"summary": "", "stack": ["python"], "category": "other"})
    c = _candidate(name="EmptySummaryProj", languages=["python"])
    result = _parse(raw, c)
    assert "EmptySummaryProj" in result.summary


def test_parse_stack_lowercased() -> None:
    raw = _json_response(stack=["Python", "Flask", "POSTGRES"])
    result = _parse(raw, _candidate())
    assert result.stack == ["python", "flask", "postgres"]


# ---------------------------------------------------------------------------
# _read_readme
# ---------------------------------------------------------------------------


def test_read_readme_reads_contents(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Hello\nThis is a project.", encoding="utf-8")
    c = _candidate(readme_path=str(readme))
    assert _read_readme(c) == "# Hello\nThis is a project."


def test_read_readme_capped_at_6000_chars(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("x" * 10_000, encoding="utf-8")
    c = _candidate(readme_path=str(readme))
    assert len(_read_readme(c, cap=6000)) == 6000


def test_read_readme_no_readme_path() -> None:
    c = _candidate(readme_path=None)
    assert _read_readme(c) == ""


def test_read_readme_missing_file() -> None:
    c = _candidate(readme_path="/does/not/exist/README.md")
    assert _read_readme(c) == ""


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_name_and_tree() -> None:
    c = _candidate(name="sample-project", file_tree=["main.py", "src/utils.py"])
    prompt = _build_prompt(c, "# Sample Project")
    assert "sample-project" in prompt
    assert "main.py" in prompt
    assert "src/utils.py" in prompt
    assert "# Sample Project" in prompt


def test_build_prompt_no_readme_placeholder() -> None:
    c = _candidate()
    prompt = _build_prompt(c, "")
    assert "(no README)" in prompt


def test_build_prompt_asks_for_json_only() -> None:
    prompt = _build_prompt(_candidate(), "")
    assert "JSON" in prompt
    assert '"summary"' in prompt
    assert '"stack"' in prompt
    assert '"category"' in prompt


# ---------------------------------------------------------------------------
# summarize() — async, mocked subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_valid_response() -> None:
    raw = _json_response("Tracks trades.", ["python", "telegram"], "trading")
    with patch(
        "orchestrator.projects.summarizer._run_subprocess",
        new=AsyncMock(return_value=raw),
    ):
        note = await summarize(_candidate(name="Sample Project", slug="sample-project"))

    assert note.summary == "Tracks trades."
    assert note.stack == ["python", "telegram"]
    assert note.category == "trading"
    assert note.name == "Sample Project"
    assert note.slug == "sample-project"
    assert note.last_indexed is not None
    assert note.discovered_at is not None


@pytest.mark.asyncio
async def test_summarize_garbage_response_does_not_raise() -> None:
    with patch(
        "orchestrator.projects.summarizer._run_subprocess",
        new=AsyncMock(return_value="oops not json"),
    ):
        note = await summarize(_candidate(name="BrokenProj", languages=["go"]))

    assert "BrokenProj" in note.summary
    assert note.category == "other"


@pytest.mark.asyncio
async def test_summarize_uses_candidate_status() -> None:
    raw = _json_response()
    c = _candidate()
    c.inferred_status = "stale"
    with patch(
        "orchestrator.projects.summarizer._run_subprocess",
        new=AsyncMock(return_value=raw),
    ):
        note = await summarize(c)

    assert note.status == "stale"


@pytest.mark.asyncio
async def test_summarize_no_readme_still_works(tmp_path: Path) -> None:
    c = _candidate(readme_path=None)
    raw = _json_response("No readme project.", ["python"], "other")
    with patch(
        "orchestrator.projects.summarizer._run_subprocess",
        new=AsyncMock(return_value=raw),
    ) as mock_run:
        note = await summarize(c)

    # Verify the prompt passed to subprocess mentions "(no README)"
    call_args = mock_run.call_args
    prompt_arg: str = call_args[0][0]
    assert "(no README)" in prompt_arg
    assert note.summary == "No readme project."


@pytest.mark.asyncio
async def test_summarize_readme_contents_in_prompt(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("Worship projector docs.", encoding="utf-8")
    c = _candidate(readme_path=str(readme))
    raw = _json_response()
    with patch(
        "orchestrator.projects.summarizer._run_subprocess",
        new=AsyncMock(return_value=raw),
    ) as mock_run:
        await summarize(c)

    prompt_arg: str = mock_run.call_args[0][0]
    assert "Worship projector docs." in prompt_arg


@pytest.mark.asyncio
async def test_summarize_readme_capped_in_prompt(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("A" * 10_000, encoding="utf-8")
    c = _candidate(readme_path=str(readme))
    raw = _json_response()
    with patch(
        "orchestrator.projects.summarizer._run_subprocess",
        new=AsyncMock(return_value=raw),
    ) as mock_run:
        await summarize(c)

    prompt_arg: str = mock_run.call_args[0][0]
    # The 10000-char blob should be capped at 6000
    assert "A" * 6001 not in prompt_arg
    assert "A" * 6000 in prompt_arg
