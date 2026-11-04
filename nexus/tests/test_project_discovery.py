"""Tests for orchestrator.projects.discovery and types."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.projects.discovery import (
    build_candidate,
    discover,
    infer_status,
    slugify,
)
from orchestrator.projects.types import ProjectCandidate, ProjectNote

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mtime_days_ago(root: Path, folder_name: str, days: int) -> None:
    """Backdate a folder's mtime by *days* from now (UTC)."""
    ts = (datetime.now(tz=UTC) - timedelta(days=days)).timestamp()
    target = root / folder_name
    os.utime(target, (ts, ts))


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_basic() -> None:
    assert slugify("Sample project") == "sample-project"


def test_slugify_lowercase() -> None:
    assert slugify("My Cool App") == "my-cool-app"


def test_slugify_special_chars() -> None:
    assert slugify("hello_world!") == "hello-world"


def test_slugify_already_kebab() -> None:
    assert slugify("nexus") == "nexus"


def test_slugify_empty_produces_project() -> None:
    assert slugify("!!!") == "project"


# ---------------------------------------------------------------------------
# infer_status
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


def test_infer_status_active_29d() -> None:
    modified = _now() - timedelta(days=29)
    assert infer_status(modified, _now()) == "active"


def test_infer_status_boundary_30d_is_stale() -> None:
    modified = _now() - timedelta(days=30)
    assert infer_status(modified, _now()) == "stale"


def test_infer_status_stale_31d() -> None:
    modified = _now() - timedelta(days=31)
    assert infer_status(modified, _now()) == "stale"


def test_infer_status_stale_179d() -> None:
    modified = _now() - timedelta(days=179)
    assert infer_status(modified, _now()) == "stale"


def test_infer_status_boundary_180d_is_archived() -> None:
    modified = _now() - timedelta(days=180)
    assert infer_status(modified, _now()) == "archived"


def test_infer_status_archived_200d() -> None:
    modified = _now() - timedelta(days=200)
    assert infer_status(modified, _now()) == "archived"


def test_infer_status_naive_datetimes() -> None:
    now_naive = datetime(2026, 6, 7, 12, 0, 0)
    modified_naive = datetime(2026, 6, 7, 12, 0, 0) - timedelta(days=5)
    assert infer_status(modified_naive, now_naive) == "active"


# ---------------------------------------------------------------------------
# discover — detection
# ---------------------------------------------------------------------------


def test_git_folder_detected(tmp_path: Path) -> None:
    proj = tmp_path / "my-project"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "README.md").write_text("# hello", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    c = results[0]
    assert ".git" in c.markers
    assert "readme" in c.markers
    assert c.has_readme is True
    assert c.readme_path is not None
    assert c.readme_path.endswith("README.md")


def test_readme_path_recorded_but_contents_absent(tmp_path: Path) -> None:
    """The candidate must know README exists and its path — never its contents."""
    proj = tmp_path / "my-project"
    proj.mkdir()
    (proj / ".git").mkdir()
    readme = proj / "README.md"
    readme.write_text("SECRET CONTENTS", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    c = results[0]
    assert c.has_readme is True
    assert c.readme_path == str(readme)
    # Ensure none of the structured fields contain the file body
    assert "SECRET CONTENTS" not in str(c.file_tree)
    assert "SECRET CONTENTS" not in str(c.markers)
    assert "SECRET CONTENTS" not in str(c.languages)


def test_pyproject_toml_detected(tmp_path: Path) -> None:
    proj = tmp_path / "py-only"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "pyproject.toml" in results[0].markers


def test_package_json_detected(tmp_path: Path) -> None:
    proj = tmp_path / "node-app"
    proj.mkdir()
    (proj / "package.json").write_text("{}", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "package.json" in results[0].markers


def test_source_files_3_plus_detected(tmp_path: Path) -> None:
    proj = tmp_path / "scripts"
    proj.mkdir()
    for i in range(3):
        (proj / f"mod{i}.py").write_text("", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "source_files>=3" in results[0].markers


def test_empty_folder_skipped(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert discover(tmp_path) == []


def test_node_modules_skipped(tmp_path: Path) -> None:
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "package.json").write_text("{}", encoding="utf-8")
    assert discover(tmp_path) == []


def test_dotfolder_skipped(tmp_path: Path) -> None:
    dot = tmp_path / ".hidden"
    dot.mkdir()
    (dot / ".git").mkdir()
    assert discover(tmp_path) == []


def test_venv_skipped(tmp_path: Path) -> None:
    for venv_name in ("venv", ".venv"):
        d = tmp_path / venv_name
        d.mkdir()
        (d / "pyproject.toml").write_text("[project]", encoding="utf-8")
    assert discover(tmp_path) == []


def test_obsidian_vault_skipped(tmp_path: Path) -> None:
    vault = tmp_path / "my-vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    (vault / "README.md").write_text("# vault", encoding="utf-8")
    assert discover(tmp_path) == []


def test_ignore_list_respected(tmp_path: Path) -> None:
    proj = tmp_path / "skip-me"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")
    assert discover(tmp_path, ignore=["skip-me"]) == []


# ---------------------------------------------------------------------------
# discover — loosened README-like matching
# ---------------------------------------------------------------------------


def test_start_here_detected_as_readme(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "START_HERE.md").write_text("# start", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert results[0].has_readme is True
    assert results[0].readme_path.endswith("START_HERE.md")
    assert "readme" in results[0].markers


def test_getting_started_detected_as_readme(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "GETTING_STARTED.md").write_text("# hi", encoding="utf-8")

    results = discover(tmp_path)
    assert results[0].has_readme is True
    assert results[0].readme_path.endswith("GETTING_STARTED.md")


def test_readme_with_prefix_detected(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "PROJECT_README.md").write_text("# hi", encoding="utf-8")

    results = discover(tmp_path)
    assert results[0].has_readme is True
    assert results[0].readme_path.endswith("PROJECT_README.md")


def test_readme_case_insensitive_variants(tmp_path: Path) -> None:
    variants = {
        "proj-a": "readme_first.txt",
        "proj-b": "start-here.txt",
        "proj-c": "GettingStarted.md",
    }
    for folder_name, filename in variants.items():
        d = tmp_path / folder_name
        d.mkdir()
        (d / ".git").mkdir()
        (d / filename).write_text("x", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 3
    assert all(c.has_readme for c in results)


def test_unrelated_filename_not_matched_as_readme(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "CHANGELOG.md").write_text("# changes", encoding="utf-8")

    results = discover(tmp_path)
    assert results[0].has_readme is False
    assert results[0].readme_path is None
    assert "readme" not in results[0].markers


# ---------------------------------------------------------------------------
# discover — one-level-deep marker detection
# ---------------------------------------------------------------------------


def test_nested_package_json_detected(tmp_path: Path) -> None:
    proj = tmp_path / "monorepo"
    proj.mkdir()
    (proj / ".git").mkdir()
    backend = proj / "backend"
    backend.mkdir()
    (backend / "package.json").write_text("{}", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "package.json (nested)" in results[0].markers
    # Top-level marker must NOT be claimed for a file that isn't at the root
    assert "package.json" not in results[0].markers


def test_nested_pyproject_toml_detected(tmp_path: Path) -> None:
    proj = tmp_path / "app"
    proj.mkdir()
    server = proj / "server"
    server.mkdir()
    (server / "pyproject.toml").write_text("[project]", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "pyproject.toml (nested)" in results[0].markers


def test_nested_source_files_3_plus_detected(tmp_path: Path) -> None:
    proj = tmp_path / "app"
    proj.mkdir()
    src = proj / "app_src"
    src.mkdir()
    for i in range(3):
        (src / f"mod{i}.py").write_text("", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "source_files>=3 (nested)" in results[0].markers


def test_nested_markers_alone_make_a_folder_a_candidate(tmp_path: Path) -> None:
    """A folder with NO top-level markers at all, only a nested one, still counts."""
    proj = tmp_path / "bare-root"
    proj.mkdir()
    inner = proj / "inner"
    inner.mkdir()
    (inner / "requirements.txt").write_text("flask", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "requirements.txt (nested)" in results[0].markers


def test_nested_scan_skips_dotfolders_and_skip_names(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()  # already a top-level marker, and must not be re-scanned as content
    (proj / ".git" / "package.json").write_text("{}", encoding="utf-8")
    nm = proj / "node_modules"
    nm.mkdir()
    (nm / "pyproject.toml").write_text("[project]", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "package.json (nested)" not in results[0].markers
    assert "pyproject.toml (nested)" not in results[0].markers


def test_nested_duplicate_marker_type_not_repeated(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    for sub_name in ("a", "b"):
        sub = proj / sub_name
        sub.mkdir()
        (sub / "package.json").write_text("{}", encoding="utf-8")

    results = discover(tmp_path)
    assert results[0].markers.count("package.json (nested)") == 1


# ---------------------------------------------------------------------------
# build_candidate — shared helper (used by manual add-by-path too)
# ---------------------------------------------------------------------------


class TestBuildCandidate:
    def test_builds_candidate_without_marker_filtering(self, tmp_path: Path) -> None:
        """A folder with zero markers must still build a valid candidate —
        this is exactly what the manual add-by-path flow relies on."""
        proj = tmp_path / "bare"
        proj.mkdir()
        (proj / "notes.txt").write_text("just notes", encoding="utf-8")

        c = build_candidate(proj)
        assert c.name == "bare"
        assert c.slug == "bare"
        assert c.path == str(proj.resolve())
        assert c.markers == []  # nothing detected, but the candidate still builds

    def test_computes_markers_when_not_provided(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")

        c = build_candidate(proj)
        assert "pyproject.toml" in c.markers

    def test_uses_provided_markers_without_rescanning(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")

        c = build_candidate(proj, markers=["custom-marker"])
        assert c.markers == ["custom-marker"]

    def test_custom_slug_overrides_slugified_name(self, tmp_path: Path) -> None:
        proj = tmp_path / "My Project"
        proj.mkdir()
        c = build_candidate(proj, slug="explicit-slug")
        assert c.slug == "explicit-slug"

    def test_default_slug_from_folder_name(self, tmp_path: Path) -> None:
        proj = tmp_path / "My Project"
        proj.mkdir()
        c = build_candidate(proj)
        assert c.slug == "my-project"


# ---------------------------------------------------------------------------
# discover — language inference
# ---------------------------------------------------------------------------


def test_languages_inferred_from_top_level(tmp_path: Path) -> None:
    proj = tmp_path / "multi"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "main.py").write_text("", encoding="utf-8")
    (proj / "app.ts").write_text("", encoding="utf-8")
    (proj / "index.js").write_text("", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    langs = results[0].languages
    assert "python" in langs
    assert "typescript" in langs
    assert "javascript" in langs


def test_languages_inferred_from_src_dir(tmp_path: Path) -> None:
    proj = tmp_path / "go-project"
    proj.mkdir()
    (proj / "src").mkdir()
    (proj / "src" / "main.go").write_text("", encoding="utf-8")
    (proj / "README.md").write_text("# go", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results) == 1
    assert "go" in results[0].languages


# ---------------------------------------------------------------------------
# discover — file_tree
# ---------------------------------------------------------------------------


def test_file_tree_names_only(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "README.md").write_text("", encoding="utf-8")
    src = proj / "src"
    src.mkdir()
    (src / "main.py").write_text("", encoding="utf-8")

    results = discover(tmp_path)
    tree = results[0].file_tree
    assert "README.md" in tree
    assert "src" in tree
    assert "src/main.py" in tree
    # No absolute paths
    for entry in tree:
        assert not entry.startswith("/")


def test_file_tree_capped_at_80(tmp_path: Path) -> None:
    proj = tmp_path / "big-proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    for i in range(200):
        (proj / f"file{i:03d}.py").write_text("", encoding="utf-8")

    results = discover(tmp_path)
    assert len(results[0].file_tree) <= 80


def test_file_tree_no_contents(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[secret config]", encoding="utf-8")

    results = discover(tmp_path)
    tree = results[0].file_tree
    # tree entries are just names, never file contents
    for entry in tree:
        assert "[secret config]" not in entry


# ---------------------------------------------------------------------------
# discover — slug uniqueness
# ---------------------------------------------------------------------------


def test_slug_collision_numeric_suffix(tmp_path: Path) -> None:
    # Two folders that normalise to the same slug
    for folder_name in ("My Project", "my-project"):
        d = tmp_path / folder_name
        d.mkdir()
        (d / "pyproject.toml").write_text("[project]", encoding="utf-8")

    results = discover(tmp_path)
    slugs = [c.slug for c in results]
    assert len(slugs) == len(set(slugs)), "Slugs must be unique"
    assert "my-project" in slugs
    assert "my-project-2" in slugs


# ---------------------------------------------------------------------------
# discover — git metadata
# ---------------------------------------------------------------------------


def _make_subprocess_result(stdout: str, returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_git_metadata_present(tmp_path: Path) -> None:
    proj = tmp_path / "gitproj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "README.md").write_text("# hi", encoding="utf-8")

    log_out = "2026-05-31T10:00:00+00:00\x1fInitial commit"
    branch_out = "main\n"
    status_out = " M dirty.py\n"

    with patch("orchestrator.projects.discovery.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_subprocess_result(log_out),       # git log
            _make_subprocess_result(status_out),    # git status
            _make_subprocess_result(branch_out),    # git rev-parse
        ]
        results = discover(tmp_path)

    assert len(results) == 1
    git = results[0].git
    assert git is not None
    assert git["branch"] == "main"
    assert git["last_commit_iso"] == "2026-05-31T10:00:00+00:00"
    assert git["last_commit_msg"] == "Initial commit"
    assert git["dirty"] is True


def test_git_metadata_clean_repo(tmp_path: Path) -> None:
    proj = tmp_path / "gitproj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "README.md").write_text("# hi", encoding="utf-8")

    log_out = "2026-01-01T00:00:00+00:00\x1fchore: initial"
    with patch("orchestrator.projects.discovery.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_subprocess_result(log_out),
            _make_subprocess_result(""),        # empty status → not dirty
            _make_subprocess_result("main\n"),
        ]
        results = discover(tmp_path)

    git = results[0].git
    assert git is not None
    assert git["dirty"] is False


def test_git_metadata_absent_when_no_git_dir(tmp_path: Path) -> None:
    proj = tmp_path / "no-git"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")

    results = discover(tmp_path)
    assert results[0].git is None


def test_git_metadata_tolerates_subprocess_timeout(tmp_path: Path) -> None:
    proj = tmp_path / "timeout-proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "README.md").write_text("", encoding="utf-8")

    with patch("orchestrator.projects.discovery.subprocess.run") as mock_run:
        import subprocess as _sp
        mock_run.side_effect = _sp.TimeoutExpired(["git"], 5)
        results = discover(tmp_path)

    assert results[0].git is None


# ---------------------------------------------------------------------------
# discover — status inference through discover()
# ---------------------------------------------------------------------------


def test_discover_active_status(tmp_path: Path) -> None:
    proj = tmp_path / "active-proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")
    _mtime_days_ago(tmp_path, "active-proj", 10)

    now = datetime.now(tz=UTC)
    results = discover(tmp_path, now=now)
    assert results[0].inferred_status == "active"


def test_discover_stale_status(tmp_path: Path) -> None:
    proj = tmp_path / "stale-proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")
    _mtime_days_ago(tmp_path, "stale-proj", 60)

    now = datetime.now(tz=UTC)
    results = discover(tmp_path, now=now)
    assert results[0].inferred_status == "stale"


def test_discover_archived_status(tmp_path: Path) -> None:
    proj = tmp_path / "old-proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]", encoding="utf-8")
    _mtime_days_ago(tmp_path, "old-proj", 200)

    now = datetime.now(tz=UTC)
    results = discover(tmp_path, now=now)
    assert results[0].inferred_status == "archived"


# ---------------------------------------------------------------------------
# ProjectNote schema
# ---------------------------------------------------------------------------


def test_project_note_defaults() -> None:
    note = ProjectNote(
        name="Sample Project",
        slug="sample-project",
        path="/Users/x/Documents/Sample project",
    )
    assert note.status == "active"
    assert note.category == "other"
    assert note.run_command is None
    assert note.preview is None
    assert note.stack == []
    assert note.summary == ""


def test_project_note_full() -> None:
    note = ProjectNote(
        name="Sample Scraper",
        slug="sample-scraper",
        path="/Users/x/Documents/Sample Scraper",
        stack=["python", "flask"],
        status="stale",
        category="scraping",
        summary="Scrapes competition winners.",
    )
    assert note.stack == ["python", "flask"]
    assert note.status == "stale"
    assert note.summary == "Scrapes competition winners."


# ---------------------------------------------------------------------------
# ProjectCandidate type integrity
# ---------------------------------------------------------------------------


def test_project_candidate_construction() -> None:
    c = ProjectCandidate(
        name="Test",
        slug="test",
        path="/tmp/test",
        last_modified="2026-06-01T00:00:00+00:00",
        languages=["python"],
        has_readme=False,
        readme_path=None,
        file_tree=["main.py"],
        git=None,
        markers=["pyproject.toml"],
        inferred_status="active",
    )
    assert c.name == "Test"
    assert c.git is None
    assert c.languages == ["python"]
