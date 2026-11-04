"""Shallow project discovery — scans a directory tree for project candidates.

Never reads file contents. Only gathers: folder names, file tree (names),
language extensions, and git metadata via subprocess.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .types import ProjectCandidate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_EXTS: frozenset[str] = frozenset(
    {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java"}
)

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
}

# Matches "readme" anywhere (any prefix/suffix — PROJECT_README, readme_first),
# "start"..."here" (START_HERE, start-here), or "getting"..."started"
# (GETTING_STARTED). Case-insensitive; searched against the whole filename.
_README_LIKE_RE = re.compile(r"readme|start.*here|getting.*started", re.IGNORECASE)

# Folders that are always skipped (in addition to dotfolders and the ignore list)
_SKIP_NAMES: frozenset[str] = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__"}
)

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_FILE_TREE_CAP = 80


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Convert a folder name to a filesystem-safe lowercase hyphenated slug.

    Does not handle collisions — callers that need uniqueness must track
    seen slugs and append a numeric suffix (``-2``, ``-3``, …) themselves.
    """
    lower = name.lower()
    slug = _SLUG_NON_ALNUM.sub("-", lower).strip("-")
    return slug or "project"


def infer_status(last_modified: datetime, now: datetime) -> str:
    """Return 'active', 'stale', or 'archived' based on age.

    active  → last_modified within 30 days of now
    stale   → 30–180 days
    archived → older than 180 days
    """
    # Normalise to offset-aware comparison
    if last_modified.tzinfo is None:
        last_modified = last_modified.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    delta_days = (now - last_modified).days
    if delta_days < 30:
        return "active"
    if delta_days < 180:
        return "stale"
    return "archived"


def discover(
    scan_root: Path,
    ignore: list[str] | None = None,
    now: datetime | None = None,
) -> list[ProjectCandidate]:
    """Scan direct children of *scan_root* and return project candidates.

    Shallow: gathers names, tree, git metadata, languages — never file contents.

    Args:
        scan_root: Directory to scan.  Defaults to ``~/Documents`` at call sites
            but is always an explicit parameter so tests can pass a temp dir.
        ignore: Additional folder names to skip.
        now: Reference timestamp for status inference; defaults to UTC now.
    """
    effective_now = now if now is not None else datetime.now(tz=UTC)
    ignore_set: frozenset[str] = frozenset(ignore or [])
    candidates: list[ProjectCandidate] = []
    seen_slugs: dict[str, int] = {}

    if not scan_root.is_dir():
        return candidates

    for child in sorted(scan_root.iterdir()):
        if not child.is_dir():
            continue
        if _should_skip(child, ignore_set):
            continue

        markers = _detect_markers(child)
        if not markers:
            continue

        slug = _unique_slug(slugify(child.name), seen_slugs)
        candidates.append(build_candidate(child, slug=slug, now=effective_now, markers=markers))

    return candidates


def build_candidate(
    folder: Path,
    slug: str | None = None,
    now: datetime | None = None,
    markers: list[str] | None = None,
) -> ProjectCandidate:
    """Build a ProjectCandidate for a single folder.

    Shared by ``discover()`` (which requires ``markers`` to be non-empty
    before including a folder) and the manual add-by-path flow (which
    bypasses marker-based filtering entirely — the caller is explicitly
    vouching for the folder being a project).

    Args:
        markers: Precomputed markers to avoid a redundant filesystem scan
            when the caller already ran ``_detect_markers``; computed fresh
            if omitted.
    """
    effective_now = now if now is not None else datetime.now(tz=UTC)
    resolved_markers = markers if markers is not None else _detect_markers(folder)
    has_readme, readme_path = _find_readme(folder)
    languages = _infer_languages(folder)
    file_tree = _build_file_tree(folder)
    git_info = _get_git_info(folder)

    mtime = folder.stat().st_mtime
    last_modified_dt = datetime.fromtimestamp(mtime, tz=UTC)
    last_modified_iso = last_modified_dt.isoformat()
    status = infer_status(last_modified_dt, effective_now)

    name = folder.name
    resolved_slug = slug if slug is not None else slugify(name)

    return ProjectCandidate(
        name=name,
        slug=resolved_slug,
        path=str(folder.resolve()),
        last_modified=last_modified_iso,
        languages=languages,
        has_readme=has_readme,
        readme_path=readme_path,
        file_tree=file_tree,
        git=git_info,
        markers=resolved_markers,
        inferred_status=status,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _should_skip(folder: Path, ignore_set: frozenset[str]) -> bool:
    name = folder.name
    if name.startswith("."):
        return True
    if name in _SKIP_NAMES:
        return True
    if name in ignore_set:
        return True
    # Skip Obsidian vaults
    if (folder / ".obsidian").is_dir():
        return True
    return False


def _detect_markers(folder: Path) -> list[str]:
    markers: list[str] = []

    if (folder / ".git").exists():
        markers.append(".git")

    has_readme, _ = _find_readme(folder)
    if has_readme:
        markers.append("readme")

    if (folder / "package.json").is_file():
        markers.append("package.json")

    for py_marker in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"):
        if (folder / py_marker).is_file():
            markers.append(py_marker)

    if (folder / "src").is_dir():
        markers.append("src/")

    # Count source files at top level
    src_count = sum(
        1
        for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in _SOURCE_EXTS
    )
    if src_count >= 3:
        markers.append("source_files>=3")

    # One level into subdirectories: some projects nest everything under
    # backend/, app/, server/, etc. rather than putting markers at the root.
    try:
        subdirs = [d for d in folder.iterdir() if d.is_dir()]
    except PermissionError:
        subdirs = []
    for sub in subdirs:
        if sub.name.startswith(".") or sub.name in _SKIP_NAMES:
            continue
        _detect_nested_markers(sub, markers)

    return markers


def _detect_nested_markers(sub: Path, markers: list[str]) -> None:
    """Check one subdirectory (already one level deep) for project markers.

    Appends ``"<marker> (nested)"`` entries to *markers* in place, skipping
    any that are already present.
    """
    try:
        entries = list(sub.iterdir())
    except PermissionError:
        return

    if (sub / "package.json").is_file() and "package.json (nested)" not in markers:
        markers.append("package.json (nested)")

    for py_marker in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"):
        tag = f"{py_marker} (nested)"
        if (sub / py_marker).is_file() and tag not in markers:
            markers.append(tag)

    nested_src_count = sum(
        1 for f in entries if f.is_file() and f.suffix.lower() in _SOURCE_EXTS
    )
    if nested_src_count >= 3 and "source_files>=3 (nested)" not in markers:
        markers.append("source_files>=3 (nested)")


def _find_readme(folder: Path) -> tuple[bool, str | None]:
    try:
        for child in sorted(folder.iterdir()):
            if child.is_file() and _README_LIKE_RE.search(child.name):
                return True, str(child)
    except PermissionError:
        pass
    return False, None


def _infer_languages(folder: Path) -> list[str]:
    """Collect languages from file extensions at top level and inside src/."""
    seen: set[str] = set()

    def _scan(directory: Path) -> None:
        try:
            for f in directory.iterdir():
                if f.is_file():
                    lang = _EXT_TO_LANG.get(f.suffix.lower())
                    if lang:
                        seen.add(lang)
        except PermissionError:
            pass

    _scan(folder)
    src = folder / "src"
    if src.is_dir():
        _scan(src)

    return sorted(seen)


def _build_file_tree(folder: Path) -> list[str]:
    """Return up to _FILE_TREE_CAP names from the top 2 levels.

    Depth-1 entries: just the name.
    Depth-2 entries: ``parent/child``.
    """
    entries: list[str] = []

    try:
        depth1 = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return entries

    for item in depth1:
        if len(entries) >= _FILE_TREE_CAP:
            break
        entries.append(item.name)
        if item.is_dir() and len(entries) < _FILE_TREE_CAP:
            try:
                depth2 = sorted(item.iterdir(), key=lambda p: (p.is_file(), p.name))
            except PermissionError:
                continue
            for sub in depth2:
                if len(entries) >= _FILE_TREE_CAP:
                    break
                entries.append(f"{item.name}/{sub.name}")

    return entries


def _get_git_info(folder: Path) -> dict[str, str | bool | None] | None:
    if not (folder / ".git").exists():
        return None

    try:
        log = subprocess.run(
            ["git", "log", "-1", "--format=%aI\x1f%s"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch_proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    branch: str | None = (
        branch_proc.stdout.strip() if branch_proc.returncode == 0 else None
    )

    last_commit_iso: str | None = None
    last_commit_msg: str | None = None
    if log.returncode == 0 and log.stdout.strip():
        parts = log.stdout.strip().split("\x1f", 1)
        if len(parts) == 2:
            last_commit_iso, last_commit_msg = parts[0], parts[1]

    dirty: bool = bool(status.stdout.strip()) if status.returncode == 0 else False

    return {
        "branch": branch,
        "last_commit_iso": last_commit_iso,
        "last_commit_msg": last_commit_msg,
        "dirty": dirty,
    }


def _unique_slug(base: str, seen: dict[str, int]) -> str:
    if base not in seen:
        seen[base] = 1
        return base
    count = seen[base] + 1
    seen[base] = count
    return f"{base}-{count}"
