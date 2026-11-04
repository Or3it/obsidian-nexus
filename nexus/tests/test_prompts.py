"""Tests for orchestrator/prompts.py — pure functions, no mocking needed."""
from orchestrator.projects.types import ProjectNote
from orchestrator.prompts import (
    build_message_history,
    build_project_context,
    build_system_prompt,
    compose_request_messages,
)
from orchestrator.types import NexusMessage, StyleProfile


def _note(name: str, category: str = "other", stack: list[str] | None = None,
          summary: str = "Does things.") -> ProjectNote:
    return ProjectNote(
        name=name,
        slug=name.lower(),
        path=f"/tmp/{name}",
        stack=stack or ["python"],
        category=category,
        summary=summary,
    )


class TestBuildSystemPrompt:
    def test_default_profile_returns_nonempty_string(self) -> None:
        result = build_system_prompt(StyleProfile())
        assert len(result) > 0

    def test_contains_all_three_style_fields(self) -> None:
        style = StyleProfile(format="structured", verbosity="detailed", posture="tutor")
        result = build_system_prompt(style)
        assert "structured" in result
        assert "detailed" in result
        assert "tutor" in result

    def test_custom_values_reflected_in_output(self) -> None:
        style = StyleProfile(format="mixed", verbosity="concise", posture="peer")
        result = build_system_prompt(style)
        assert "mixed" in result
        assert "concise" in result
        assert "peer" in result


class TestBuildMessageHistory:
    def test_empty_history_returns_single_user_message(self) -> None:
        result = build_message_history([], "hello")
        assert result == [NexusMessage(role="user", content="hello")]

    def test_existing_history_appended_correctly(self) -> None:
        prior_user = NexusMessage(role="user", content="first")
        prior_assistant = NexusMessage(role="assistant", content="reply")
        result = build_message_history([prior_user, prior_assistant], "follow-up")
        assert len(result) == 3
        assert result[0] == prior_user
        assert result[1] == prior_assistant
        assert result[2] == NexusMessage(role="user", content="follow-up")

    def test_does_not_mutate_input_history(self) -> None:
        history = [NexusMessage(role="user", content="first")]
        build_message_history(history, "second")
        assert len(history) == 1


class TestComposeRequestMessages:
    def test_system_message_is_first(self) -> None:
        msgs = compose_request_messages([], "hi", StyleProfile())
        assert msgs[0].role == "system"

    def test_user_message_is_last(self) -> None:
        msgs = compose_request_messages([], "hi", StyleProfile())
        assert msgs[-1].role == "user"
        assert msgs[-1].content == "hi"

    def test_history_preserved_between_system_and_new_user(self) -> None:
        history = [
            NexusMessage(role="user", content="old"),
            NexusMessage(role="assistant", content="response"),
        ]
        msgs = compose_request_messages(history, "new", StyleProfile())
        roles = [m.role for m in msgs]
        assert roles == ["system", "user", "assistant", "user"]

    def test_project_context_becomes_second_system_message(self) -> None:
        msgs = compose_request_messages(
            [], "hi", StyleProfile(), project_context="- proj digest"
        )
        roles = [m.role for m in msgs]
        assert roles == ["system", "system", "user"]
        assert "- proj digest" in msgs[1].content
        assert "Reference" in msgs[1].content

    def test_no_project_context_message_when_absent(self) -> None:
        msgs = compose_request_messages([], "hi", StyleProfile(), project_context=None)
        assert [m.role for m in msgs] == ["system", "user"]


class TestBuildProjectContext:
    def test_empty_notes_returns_empty_string(self) -> None:
        assert build_project_context([]) == ""

    def test_one_line_per_project_with_fields(self) -> None:
        notes = [
            _note("Alpha", category="trading", stack=["python", "flask"]),
            _note("Beta", category="worship"),
        ]
        digest = build_project_context(notes)
        lines = digest.split("\n")
        assert len(lines) == 3  # header + 2 projects
        assert "Alpha [trading; python, flask]" in lines[1]
        assert "Beta [worship; python]" in lines[2]
        assert "Does things." in lines[1]

    def test_caps_at_max_projects(self) -> None:
        notes = [_note(f"P{i}") for i in range(40)]
        digest = build_project_context(notes, max_projects=25)
        assert len(digest.split("\n")) == 26  # header + 25

    def test_trims_summaries_and_flattens_newlines(self) -> None:
        long_summary = ("line one\nline two " * 50).strip()
        digest = build_project_context([_note("Big", summary=long_summary)], summary_cap=240)
        line = digest.split("\n")[1]
        assert "\n" not in line
        # name/category/stack prefix + capped summary
        assert len(line) < 320
