# P3A-T01 — Project Discovery + Project-Note Schema

## Goal

Scan `~/Documents/` for project folders, gather lightweight signals about each, and return them as candidates — without reading file contents (privacy: deep reads happen only at summarize time on approved projects). Define the project-note schema. No summarization, no UI, no storage of approved projects yet.

## Read first

- `nexus/orchestrator/vault/frontmatter.py` (note read/write pattern)
- `nexus/orchestrator/vault/projection.py` (how notes are written to the vault)
- `nexus-build/phase-3a/00-OVERVIEW.md` (schema + scope)

## Files to create

```
nexus/orchestrator/projects/__init__.py
nexus/orchestrator/projects/discovery.py
nexus/orchestrator/projects/types.py
nexus/tests/test_project_discovery.py
```

## Detection rule

A direct child folder of the scan root is a **candidate** if it contains any of:
- `.git/`
- a README (`README`, `README.md`, `README.txt`, case-insensitive)
- `package.json`
- `pyproject.toml`, `requirements.txt`, `setup.py`, `Pipfile`
- a `src/` dir, or ≥3 source files (`.py .ts .js .tsx .jsx .rs .go .java`) at top level

Skip: dotfolders, `node_modules`, `.venv`/`venv`, `__pycache__`, the vault itself, and anything matching a configurable ignore list.

## Signals gathered per candidate (shallow only)

```python
@dataclass
class ProjectCandidate:
    name: str                 # folder name, human-cased
    slug: str                 # filesystem-safe, lowercased, hyphenated
    path: str                 # absolute
    last_modified: str        # ISO, from folder mtime
    languages: list[str]      # inferred from top-level + src/ file extensions
    has_readme: bool
    readme_path: str | None   # path only — NOT contents
    file_tree: list[str]      # top 2 levels, names only, capped (~80 entries)
    git: dict | None          # {branch, last_commit_iso, last_commit_msg, dirty} or None
    markers: list[str]        # which detection markers matched
    inferred_status: str      # active (mtime<30d) | stale (<180d) | archived (older)
```

Read folder mtime, run `git log -1 --format=...` and `git status --porcelain` if `.git/` exists (via subprocess, short timeout, tolerate failure), list the top-2-level tree (names only). **Do not open README or any source file contents** — only record that the README exists and its path.

## `discovery.py`

```python
def discover(scan_root: Path, ignore: list[str] | None = None) -> list[ProjectCandidate]:
    """Scan direct children of scan_root, return project candidates.
    Shallow: gathers names, tree, git metadata, languages — never file contents."""
    ...

def slugify(name: str) -> str:
    """'Sample project' -> 'sample-project'. Filesystem-safe, unique-able."""
    ...

def infer_status(last_modified: datetime, now: datetime) -> str:
    """active <30d, stale <180d, else archived."""
    ...
```

`scan_root` defaults to `~/Documents` but is a parameter (so tests use a temp dir).

## Schema definition (types only here; storage in T02)

Define a `ProjectNote` pydantic model matching the frontmatter in the overview (name, slug, path, stack, status, category, run_command, preview, last_indexed, discovered_at + summary body). T02 writes it; this ticket just defines it.

## Tests

`tests/test_project_discovery.py` — build a temp dir tree:
- Folder with `.git/` + README → detected, markers include both, has_readme True, readme contents NOT in the candidate.
- Folder with only `pyproject.toml` → detected.
- Empty folder / `node_modules` / dotfolder → skipped.
- Languages inferred from file extensions present.
- `slugify("Sample project") == "sample-project"`; collisions get a numeric suffix.
- `infer_status` boundaries (29d active, 31d stale, 200d archived) with injected `now`.
- file_tree capped and names-only (no contents).
- git metadata parsed when `.git` present (mock the subprocess), None when absent.

## Definition of done

- [ ] `discover()` returns candidates from a scan root, shallow only (no file contents read).
- [ ] `ProjectCandidate` and `ProjectNote` types defined.
- [ ] slug/status/language inference work and are tested.
- [ ] All tests pass; `ruff` + `mypy` clean.
- [ ] Manual: run `discover(Path.home()/"Documents")` in a REPL, confirm it returns your ~12 active projects with sensible names/slugs/status and no README contents in the output.

## Definition of NOT done

- No summarization (T02). No README/source contents read.
- No endpoints (T03). No storage of approved projects yet.
- No UI.
