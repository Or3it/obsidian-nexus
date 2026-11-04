# P3A-T05 — Discover → Review → Approve Flow

## Goal

The human gate. "Add projects" scans `~/Documents`, shows discovered-but-not-indexed candidates, you check the ones to include, and approving summarizes + stores them — after which they appear on the wall. Nothing is indexed without your approval.

## Read first

- T03 (POST /projects/discover, POST /projects/index)
- T04 (DashboardView, the inert "Add projects" button)
- `nexus/plugin/lib/nexus-client.ts`

## Files to modify

```
nexus/plugin/lib/nexus-client.ts       ← discoverProjects(), indexProjects(slugs)
nexus/plugin/views/dashboard-view.ts   ← review modal + wire the Add button
nexus/plugin/styles.css                ← review modal styling (glass)
```

## Flow

1. Click **Add projects** → call `client.discoverProjects()` → get candidates not yet indexed.
2. Open a glass modal listing candidates. Each row: checkbox, name, path, inferred status, languages, markers (why it was detected). Pre-check `active` ones, leave `stale`/`archived` unchecked (sensible default; user can change).
3. **Approve selected** → call `client.indexProjects(checkedSlugs)`. Show per-project progress (summarizing… ✓/✗) since each is a subprocess summary.
4. On completion, close the modal and refresh the wall (re-call listProjects) so the new cards appear.
5. Empty state: if discover returns nothing new, show "No new projects found in ~/Documents."

## Modal

Use Obsidian's `Modal` class (themed, dismissible) with `.nexus-glass` applied to the content. Rows are simple; the per-project progress can be inline status text updated as the index call returns per-slug results. If `indexProjects` is a single call returning all results, show a spinner then the summary; if you stream, update rows live — single-call-then-render is fine for v1.

```css
.nexus-review-modal .modal-content { /* glass */ }
.nexus-review-row {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 4px; border-bottom: 1px solid color-mix(in srgb, var(--background-modifier-border) 40%, transparent);
}
.nexus-review-row .meta { font-size: 0.75em; color: var(--text-muted); }
.nexus-review-progress { font-size: 0.78em; }
.nexus-review-progress.ok { color: var(--color-green); }
.nexus-review-progress.err { color: var(--color-red); }
```

## Behaviour rules

- Already-indexed projects never appear in the candidate list (T03 filters them).
- Re-running discover after approving shows the remaining un-indexed ones.
- If a summary fails for one project, mark that row errored and continue the others (don't abort the batch).
- The wall refreshes only after the modal action completes.

## Definition of done

- [ ] `discoverProjects()` / `indexProjects()` client methods.
- [ ] "Add projects" opens the review modal with candidates + sensible default checks.
- [ ] Approving indexes the selected projects (subprocess summaries) and refreshes the wall.
- [ ] Per-project ok/error feedback; one failure doesn't abort the batch.
- [ ] Empty state handled.
- [ ] `tsc --noEmit` clean; built + copied to the test vault.
- [ ] Manual: with zero or few projects indexed, click Add projects, approve 3-4, watch them summarize and appear as cards.

## Definition of NOT done

- No editing summaries in the modal (open the note to edit; v1).
- No run_command capture here (3B).
- No detail view (T06).
