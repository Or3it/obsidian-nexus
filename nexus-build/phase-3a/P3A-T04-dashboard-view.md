# P3A-T04 — Dashboard View (Glass Card Wall)

## Goal

A new Obsidian view: a scrollable glass wall of all indexed project cards. This is the home base — open it and see everything. Read-only in this ticket (discovery/approve is T05, click-in detail is T06).

## Read first

- `nexus/plugin/views/chat-view.ts` (ItemView registration, lifecycle, client usage)
- `nexus/plugin/main.ts` (view registration, ribbon/command)
- `nexus/plugin/lib/nexus-client.ts` (add project methods)
- `nexus/plugin/styles.css` (the `.nexus-glass` token from P2c-T06)

## Files to modify

```
nexus/plugin/views/dashboard-view.ts    ← new ItemView
nexus/plugin/main.ts                     ← register view + ribbon icon + command
nexus/plugin/lib/nexus-client.ts         ← listProjects() (GET /projects)
nexus/plugin/styles.css                  ← card wall + glass card styles
```

## The view

Register `DashboardView` (VIEW_TYPE_NEXUS_DASHBOARD) like the chat view. Add:
- a ribbon icon ("layout-dashboard") that opens it
- a command "Open Nexus Dashboard"
- opens in the main workspace area (not the sidebar) — it's a full home page, not a side panel

On open: call `client.listProjects()`, render the card wall.

## Card wall layout

- A responsive grid of glass cards, scrollable, all projects visible (no forced grouping per your call — one flowing wall).
- Each card (`.nexus-project-card.nexus-glass`):
  - **Preview slot** at top: reserved area. In 3A, if the note has a `preview` value that's an image path, show it; otherwise show a generated monogram/gradient placeholder (project initial on a tinted glass panel). The live-UI preview is 3B — the slot is designed in now.
  - **Name** (prominent)
  - **Summary** (2-3 lines, truncated with ellipsis)
  - **Stack badges** (small glass pills, e.g. python · flask · telegram)
  - **Status dot** (active green / stale amber / archived grey) + last-activity relative time
  - Whole card is clickable (wired to the detail view in T06; in T04 it can console.log / no-op).
- A small header: "Projects" + count, and an "Add projects" button (wired in T05; present but inert here).

## Styling (liquid glass)

Build on `.nexus-glass`. Cards should feel like frosted panels:

```css
.nexus-dashboard { padding: 24px; overflow-y: auto; height: 100%; }
.nexus-dashboard-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 20px;
}
.nexus-card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 18px;
}
.nexus-project-card {
    /* inherits .nexus-glass */
    padding: 0; overflow: hidden; cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
    display: flex; flex-direction: column;
}
.nexus-project-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 12px 40px rgba(0,0,0,0.25);
}
.nexus-card-preview {
    height: 120px; display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg,
        color-mix(in srgb, var(--interactive-accent) 30%, transparent),
        color-mix(in srgb, var(--background-secondary) 60%, transparent));
    font-size: 2.4em; font-weight: 700; color: var(--text-on-accent);
}
.nexus-card-body { padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; }
.nexus-card-name { font-weight: 650; font-size: 1.05em; }
.nexus-card-summary {
    font-size: 0.85em; color: var(--text-muted);
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.nexus-stack { display: flex; flex-wrap: wrap; gap: 5px; }
.nexus-stack-pill {
    font-size: 0.68em; padding: 2px 8px; border-radius: 999px;
    background: color-mix(in srgb, var(--background-modifier-border) 50%, transparent);
}
.nexus-card-foot { display: flex; align-items: center; gap: 6px; font-size: 0.72em; color: var(--text-faint); }
.nexus-status-dot { width: 8px; height: 8px; border-radius: 50%; }
.nexus-status-dot.active { background: var(--color-green); }
.nexus-status-dot.stale { background: var(--color-yellow); }
.nexus-status-dot.archived { background: var(--text-faint); }
```

## Tests / verification

No TS test harness in this repo — verification is manual (DoD below). Keep render logic in small pure helpers (e.g. `relativeTime(iso)`, `monogram(name)`) and you may add a tiny unit test file if trivial, but it's optional.

## Definition of done

- [ ] DashboardView registered; ribbon icon + "Open Nexus Dashboard" command open it in the main area.
- [ ] Cards render from GET /projects: preview slot (image or monogram), name, summary, stack pills, status dot, relative time.
- [ ] Scrollable responsive grid; glass styling applied.
- [ ] `tsc --noEmit` clean; `npm run build`; main.js + styles.css copied to `~/nexus-test-vault/.obsidian/plugins/obsidian-nexus/`.
- [ ] Manual: with a few projects indexed (from T03), open the dashboard and see the glass card wall; cards show real summaries/stacks/status.

## Definition of NOT done

- Card click → detail view is T06 (no-op/log here).
- "Add projects" flow is T05 (button present, inert).
- Live preview in the slot is 3B (monogram/image placeholder only).
- No sections/filtering — one flowing wall (a filter could come later if needed).
