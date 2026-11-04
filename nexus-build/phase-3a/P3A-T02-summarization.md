# P3A-T02 — Summarization via Subprocess

## Goal

Turn an approved candidate into a stored project note: feed its README + file tree to Sonnet via the Claude Code subprocess (free, Pro subscription), get back a one-paragraph summary + detected stack + inferred category, and write `Projects/<slug>.md` to the vault.

## Read first

- T01 (`ProjectCandidate`, `ProjectNote`)
- `nexus/orchestrator/adapters/anthropic.py` (the subprocess invocation pattern — `claude --print`)
- `nexus/orchestrator/vault/frontmatter.py` + `projection.py` (note writing)

## Files to create / modify

```
nexus/orchestrator/projects/summarizer.py     ← new
nexus/orchestrator/projects/store.py          ← new (write/read/delete Projects/*.md)
nexus/tests/test_project_summarizer.py
nexus/tests/test_project_store.py
```

## Summarization

Now (at summarize time, on an approved project only) read the README contents and assemble a compact prompt. Keep it bounded — cap README at ~6000 chars, file tree at ~80 entries.

```python
async def summarize(candidate: ProjectCandidate) -> ProjectSummary:
    """Generate summary + stack + category via the Claude Code subprocess.
    Free (Pro subscription). Returns structured fields parsed from the model output."""
    readme = _read_readme(candidate, cap=6000)        # contents read HERE, post-approval
    prompt = _build_prompt(candidate, readme)
    raw = await _run_subprocess(prompt)               # claude --print, JSON out
    return _parse(raw, candidate)
```

Prompt asks for strict JSON to avoid parsing fragility:

```
You are cataloguing a software project for a personal dashboard.
Given the README and file tree, respond with ONLY a JSON object, no prose:
{
  "summary": "<2-3 sentence plain-English description of what this project does>",
  "stack": ["<key technologies/frameworks, lowercase>"],
  "category": "<one of: trading, ai-tooling, scraping, worship, other>"
}

Project name: {name}
File tree:
{tree}
README:
{readme or "(no README)"}
```

Parse defensively: strip markdown fences, `json.loads`, fall back to a minimal summary (folder name + languages) if parsing fails so a bad model response never blocks indexing. `status` comes from T01's mtime inference, not the model.

Use the existing subprocess invocation (`claude --print --output-format ...`) — the same path that already works on the Pro subscription. Do NOT use the paid SDK path.

## Store (`store.py`)

```python
def write_project_note(vault_root: Path, note: ProjectNote) -> Path: ...
def read_project_notes(vault_root: Path) -> list[ProjectNote]: ...   # reads Projects/*.md
def read_project_note(vault_root: Path, slug: str) -> ProjectNote | None: ...
def delete_project_note(vault_root: Path, slug: str) -> bool: ...     # removes note, NOT the real folder
```

Notes live in `{vault_root}/Projects/`. Frontmatter per the overview schema; summary is the body. Idempotent: re-indexing a slug overwrites its note and refreshes `last_indexed`, preserving `discovered_at` and any user-edited `run_command`.

## Tests

`tests/test_project_summarizer.py` (mock the subprocess):
- Valid JSON response → ProjectSummary with summary/stack/category.
- Response wrapped in ```json fences → still parsed.
- Garbage response → fallback summary (name + languages), no exception.
- README capped at 6000 chars before prompt build.
- No README → prompt says "(no README)", still produces a summary.

`tests/test_project_store.py` (temp vault):
- write then read round-trips all frontmatter fields + body.
- read_project_notes returns all notes in Projects/.
- re-write same slug overwrites, refreshes last_indexed, preserves discovered_at + run_command.
- delete removes the note file and returns True; missing slug returns False.
- delete never touches anything outside Projects/.

## Definition of done

- [ ] `summarize()` produces structured summary via subprocess; defensive JSON parsing with fallback.
- [ ] `store.py` writes/reads/deletes `Projects/*.md` notes idempotently.
- [ ] README contents read only here (post-approval), capped.
- [ ] All tests pass; `ruff` + `mypy` clean.
- [ ] Manual: summarize one real project (e.g. sample-app), confirm `Projects/sample-app.md` has a sensible Sonnet summary, correct stack, category=worship.

## Definition of NOT done

- No endpoints (T03). No UI.
- No embeddings / code-level indexing — summary only.
- Don't read source file contents — README + tree only.
