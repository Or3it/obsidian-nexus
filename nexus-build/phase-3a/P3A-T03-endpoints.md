# P3A-T03 — Project Endpoints

## Goal

Expose discovery, indexing, listing, removal, and editor-handoff over HTTP so the plugin can drive the hub. Wires T01 (discovery) + T02 (summarizer/store) behind the orchestrator's API.

## Read first

- `nexus/orchestrator/server.py` (endpoint + auth pattern)
- `nexus/orchestrator/orchestrator.py` (where to add project methods)
- T01, T02

## Endpoints

```
GET    /projects                 → list indexed projects (from Projects/*.md)
POST   /projects/discover        → scan ~/Documents, return candidates NOT yet indexed
POST   /projects/index           → body: {slugs: [...]}; summarize + store each approved candidate
DELETE /projects/{slug}          → remove the project note (NOT the folder)
POST   /projects/{slug}/open     → body: {target: "vscode"|"finder"}; open the project there
```

All require the existing bearer auth.

### GET /projects
Returns `read_project_notes()` as JSON: each note's frontmatter + summary. Sorted by status (active first) then last_modified desc.

### POST /projects/discover
Runs `discover(scan_root)`, filters out any slug that already has a note, returns the remaining candidates. This is the "what's new to add" list for the review UI. Does not store anything.

### POST /projects/index
Body `{slugs: [...]}`. For each slug, re-discover (or accept the candidate passed in), run `summarize()`, `write_project_note()`. Return the created notes. This is the action behind "approve these projects." Long-ish (a subprocess summary per project) — stream progress or return when all done; for v1, return when done with a per-slug ok/error result.

### POST /projects/{slug}/open
Body `{target}`. Resolve the project's path from its note, then:
- `vscode` → `code <path>` via subprocess
- `finder` → `open <path>` (macOS) via subprocess
Return ok/error. Guard: the path must be inside the configured scan root (no arbitrary path open). 404 if slug unknown.

## Files to modify

```
nexus/orchestrator/orchestrator.py     ← project methods (discover/list/index/remove/open)
nexus/orchestrator/server.py           ← the five routes
nexus/tests/test_server.py             ← endpoint tests
nexus/tests/test_orchestrator.py       ← project-method tests
```

## Config

Add a `projects` block (optional) to cascade.yaml or a small config:
```yaml
projects:
  scan_root: /Users/you/Documents
  ignore: [nexus-test-vault, node_modules]
```
Default scan_root to `~/Documents` if absent.

## Tests

`tests/test_orchestrator.py`:
- discover returns candidates excluding already-indexed slugs.
- index summarizes (mock subprocess) + writes notes for given slugs; returns per-slug results.
- remove deletes the note, returns True; unknown slug False.
- open resolves path and calls the right command (mock subprocess) for vscode/finder; rejects a path outside scan_root; 404 unknown slug.

`tests/test_server.py`:
- GET /projects returns indexed notes, sorted.
- POST /projects/discover returns only un-indexed candidates.
- POST /projects/index with slugs → notes created.
- DELETE /projects/{slug} → 200/404.
- POST /projects/{slug}/open with target → ok; bad target → 400.
- All endpoints 401 without auth.

## Definition of done

- [ ] Five endpoints implemented and authed.
- [ ] open guards against paths outside scan_root.
- [ ] All tests pass; `ruff` + `mypy` clean.
- [ ] Manual: `curl POST /projects/discover` returns your un-indexed projects; index two of them; GET /projects shows them; open one in vscode; delete one.

## Definition of NOT done

- No run/start-stop (Phase 3B). open = editor/finder handoff only.
- No UI (T04+).
- No streaming progress on index (v1 returns when done).
