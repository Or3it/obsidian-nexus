# P3A-T08 — Glass Polish + Acceptance

## Goal

Final visual polish (and fold in the carried-over routing-panel overlap fix), then verify all eight Phase 3A acceptance criteria and sign off.

## Read first

- `phase-3a/00-OVERVIEW.md` (the eight criteria)
- `OPEN_QUESTIONS.md` (the T06 routing-panel overlap note from Phase 2c)
- `docs/phase-2c-acceptance-report.md` (template/tone)

## Polish

1. **Liquid-glass refinement.** Tune `.nexus-glass` and the card/detail styling toward the iPhone-liquid-glass feel: layered translucency, soft inner highlight, gentle depth on hover. Keep it readable in both light and dark Obsidian themes (test both). Specifics:
   - Add a subtle 1px inner top highlight (`box-shadow: inset 0 1px 0 rgba(255,255,255,0.12)`) for the "glass edge" look.
   - Ensure `backdrop-filter` degrades gracefully where unsupported (solid translucent fallback).
   - Card hover lift consistent across the wall.
2. **Routing-panel overlap fix (carried from Phase 2c OPEN_QUESTIONS).** The cascade routing-status panel overlaps the chat transcript. Fix: raise z-index and give it an opaque-enough glass backdrop, or anchor it so it doesn't sit on top of messages. Verify the picker still dismisses on click-outside.
3. **Empty/loading states.** Dashboard with no projects → friendly empty state with an "Add projects" CTA. Loading → glass skeleton cards, not a blank screen.
4. **Monogram placeholders** look intentional (tinted per category, project initial), since most of your projects won't have a visual preview in 3A.

## Acceptance

Write `docs/phase-3a-acceptance-report.md`. Verify each criterion with evidence:

```markdown
# Phase 3A Acceptance Report
Date: ____  Env: macOS __ | Python __ | Plugin: P3A-T08
Backend: ~/Documents/nexus/nexus  Vault: ~/nexus-test-vault

## Criteria
- [ ] 1. pytest tests/ passes — `__ passed, __ skipped`.
- [ ] 2. Discovery finds active ~/Documents projects as candidates; no file contents read at discover time. *probe output*
- [ ] 3. Approving a candidate writes Projects/<slug>.md with a real Sonnet summary + stack + status/category. *note screenshot*
- [ ] 4. Dashboard renders a scrollable glass wall of all indexed cards. *screenshot, light + dark*
- [ ] 5. Clicking a card opens detail (summary, tree, README, git) inside Obsidian. *screenshot*
- [ ] 6. Open-in-VS-Code and Reveal-in-Finder work from detail. *confirm*
- [ ] 7. Project-aware chat answers a cross-project question from summaries. *transcript*
- [ ] 8. Removing a project deletes its note + card, real folder untouched. *confirm folder still exists*

## Polish verification
- Glass look in light + dark themes
- Routing-panel overlap resolved
- Empty/loading states

## Drift / new OPEN_QUESTIONS entries
## Sign-off — 3A ready, 3B (live/run layer) clear to plan: ☐
```

## OPEN_QUESTIONS updates

- Mark the routing-panel overlap RESOLVED (with the fix).
- Note 3A shipped summary-level; record the criteria for deciding whether code-level retrieval (the deferred FAISS work) is ever needed — i.e. only if summary answers prove too shallow in real use.
- Record the 3B scope (run_command capture, start/stop with streamed output, live web-UI previews) as the next planned phase.

## Definition of done

- [ ] Glass polish applied; readable in light + dark.
- [ ] Routing-panel overlap fixed and marked resolved.
- [ ] Empty/loading/monogram states done.
- [ ] Acceptance report filled, all 8 criteria green.
- [ ] OPEN_QUESTIONS updated.
- [ ] One commit: `feat(phase-3a): project hub acceptance + glass polish`.
- [ ] Do not start 3B until the report is committed.

## Definition of NOT done

- No 3B features (run/preview).
- No new project capabilities beyond polish + acceptance.
