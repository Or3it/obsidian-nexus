# P3A-T07 — Project-Aware Chat

## Goal

Let the existing Nexus chat answer questions about your projects by injecting the approved project summaries into context. One chat engine, now project-aware — ask "which projects use Flask?", "what does sample-scraper do?", "what was I building in sample project?" and get grounded answers from the summaries.

## Read first

- `nexus/orchestrator/orchestrator.py` (turn flow, context/system assembly)
- `nexus/orchestrator/prompts.py` (how system context is built)
- T02 store (read_project_notes), T03 (GET /projects)

## Design

Do NOT build a second chat. Add an optional **project context** to the existing turn flow:
- A toggle/flag on the turn: "include project context."
- When on, the orchestrator reads `Projects/*.md` and injects a compact digest into the system context — one line per project: `name (category, stack) — summary`. Capped (e.g. top ~25 projects, summaries trimmed) so it fits comfortably.
- The chat then answers using that digest. Summary-level by design (it knows what each project is, not its code).

Two entry points, one engine:
- From the **dashboard**: a chat affordance (button/panel) that opens the chat with project context ON.
- From the **normal chat**: a small toggle ("Projects" context) so you can opt in there too.

## Files to modify

```
nexus/orchestrator/orchestrator.py     ← assemble project digest, inject when flag set
nexus/orchestrator/prompts.py          ← project-context section builder
nexus/orchestrator/server.py           ← accept include_projects flag on the turn payload
nexus/plugin/lib/nexus-client.ts       ← pass include_projects on sendTurn
nexus/plugin/views/chat-view.ts        ← "Projects" context toggle
nexus/plugin/views/dashboard-view.ts   ← "Ask about my projects" affordance → opens chat w/ flag on
nexus/tests/test_orchestrator.py
nexus/tests/test_prompts.py
```

## Digest builder (`prompts.py`)

```python
def build_project_context(notes: list[ProjectNote], max_projects: int = 25,
                          summary_cap: int = 240) -> str:
    """Compact one-line-per-project digest for the system context."""
    if not notes:
        return ""
    lines = ["The user's projects (summary-level knowledge):"]
    for n in notes[:max_projects]:
        stack = ", ".join(n.stack[:5])
        summ = (n.summary or "").strip().replace("\n", " ")[:summary_cap]
        lines.append(f"- {n.name} [{n.category}; {stack}] — {summ}")
    return "\n".join(lines)
```

Inject this block into the system context only when `include_projects` is set, ahead of the user message, clearly delimited so the model treats it as reference.

## Turn payload

`POST /turn` accepts an optional `include_projects: bool` (default false). When true, orchestrator builds the digest and prepends it to context for that turn. Stateless per turn (no persistent mode) — simplest and predictable.

## Tests

`tests/test_prompts.py`:
- `build_project_context([])` → "".
- digest formats one line per project with name/category/stack/trimmed summary.
- caps at max_projects; trims summaries at summary_cap.

`tests/test_orchestrator.py`:
- turn with `include_projects=true` injects the digest into context (assert the digest text reaches the adapter request).
- turn with flag false → no project context (unchanged behavior).
- digest reflects current Projects/*.md (add a note, next flagged turn includes it).

## Definition of done

- [ ] `include_projects` flag on the turn payload; digest injected when true.
- [ ] Digest is compact, capped, one line per project.
- [ ] Dashboard "Ask about my projects" opens the chat with the flag on; chat has a "Projects" toggle too.
- [ ] Flag false = identical to current behavior.
- [ ] All tests pass; `tsc --noEmit` clean; built + copied.
- [ ] Manual: index several projects, ask "which of my projects involve trading?" and "what does sample-app do?" with the flag on — answers reflect the summaries.

## Definition of NOT done

- No code-level retrieval — summaries only (a project asks "what does X do", not "show me X's auth code").
- No persistent always-on project mode — per-turn flag.
- No per-project scoped chat ("chat about ONLY this project") — that's a nice 3B/later add; v1 injects all summaries.
