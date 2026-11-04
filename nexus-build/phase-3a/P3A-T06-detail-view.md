# P3A-T06 — Click-In Detail View + VS Code/Finder Handoff

## Goal

Click a card → it expands into a detail view inside Obsidian: summary, file tree, rendered README, git status — read it all without leaving. Plus handoff buttons (open in VS Code / reveal in Finder) for when you do need to leave. This is the "Obsidian as home base" core: everything visible in-app, exits only when you choose.

## Read first

- T03 (GET /projects, POST /projects/{slug}/open)
- T04 (DashboardView, card click currently no-op)
- T02 store (project notes have the summary + frontmatter)

## What's needed from the backend

The detail view needs more than the summary note holds: the README contents and the live file tree. Two options — pick the simpler:
- **(preferred)** Add `GET /projects/{slug}/detail` returning `{note, readme_html_or_md, file_tree, git}` — server reads the folder fresh so the tree/README are current. Small addition to T03's surface.
- Or reuse discovery for tree/git and read README on demand. The endpoint is cleaner; add it.

Add `client.getProjectDetail(slug)`.

## Files to modify

```
nexus/orchestrator/server.py             ← GET /projects/{slug}/detail
nexus/orchestrator/orchestrator.py       ← assemble detail (note + README + tree + git)
nexus/plugin/lib/nexus-client.ts         ← getProjectDetail(slug), openProject(slug, target)
nexus/plugin/views/dashboard-view.ts     ← detail panel + card-click wiring
nexus/plugin/styles.css                  ← detail panel (glass)
nexus/tests/test_server.py               ← detail endpoint test
```

## Detail view (inside the dashboard)

Clicking a card transitions the dashboard to a detail layout (or slides a glass panel over the wall, with a back button). Contents:
- **Header**: name, status dot, category, stack pills, "← Back to projects".
- **Summary**: the Sonnet summary, full (not truncated).
- **Actions row**: `Open in VS Code` · `Reveal in Finder` · `Re-index` (re-summarize) · `Remove`.
- **README**: rendered (markdown → Obsidian's renderer, or the server returns HTML). Scrollable.
- **File tree**: the top-2-level tree as an indented list.
- **Git**: branch, last commit (msg + relative time), dirty/clean.

The README rendering: prefer using Obsidian's `MarkdownRenderer.render` on the raw README markdown returned by the endpoint — gives native styling for free.

## Handoff buttons

- `Open in VS Code` → `client.openProject(slug, "vscode")` → POST /projects/{slug}/open. Toast on success/failure.
- `Reveal in Finder` → same with `"finder"`.
- `Re-index` → calls index for this slug again (refreshes the summary), reloads the detail.
- `Remove` → confirm, then DELETE /projects/{slug}, return to the wall (folder untouched — say so in the confirm dialog).

## Styling

```css
.nexus-detail { padding: 24px; overflow-y: auto; height: 100%; }
.nexus-detail-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.nexus-detail-actions { display: flex; gap: 10px; margin: 16px 0; flex-wrap: wrap; }
.nexus-detail-actions button { /* glass buttons */ }
.nexus-detail-section { margin-top: 20px; }
.nexus-detail-section h3 { font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }
.nexus-file-tree { font-family: var(--font-monospace); font-size: 0.8em; color: var(--text-muted); }
.nexus-readme { /* rendered markdown container */ }
```

## Tests

`tests/test_server.py`:
- GET /projects/{slug}/detail returns note + readme + tree + git for an indexed slug.
- 404 for unknown slug.
- detail reads the folder fresh (tree reflects current state — test by adding a file to a temp project between index and detail).

Plugin: manual (DoD).

## Definition of done

- [ ] GET /projects/{slug}/detail endpoint + client method.
- [ ] Clicking a card opens the detail view inside Obsidian with summary, rendered README, file tree, git.
- [ ] Back button returns to the wall.
- [ ] Open in VS Code / Reveal in Finder / Re-index / Remove all work; Remove confirms and never touches the real folder.
- [ ] Server tests pass; `tsc --noEmit` clean; built + copied to test vault.
- [ ] Manual: click into 2-3 projects, read README + tree in Obsidian, open one in VS Code, re-index one, remove a test one.

## Definition of NOT done

- No in-Obsidian code editing — read-only view; editing is the VS Code handoff.
- No run/start-stop (3B).
- No live UI preview (3B).
