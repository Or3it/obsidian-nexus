# Phase 1 — Single-Model Spine

## Goal

A working conversation with Claude Sonnet 4.6 via Claude Code via Ruflo via the user's Pro subscription, persisting to the vault and surviving plugin restart.

**What "done" looks like:** open the Obsidian plugin chat view, type a message, watch Sonnet stream a reply, see the conversation appear as a markdown file in `Orchestrator/Live/`, close Obsidian, reopen, see the conversation still there and resumable.

No cascade. No failover. No retrieval. No quota indicator. No Opus button. Just one model, one happy path, plus the bones of the persistence layer that everything else will hang off.

## Phase 1 global acceptance criteria

All seven must be green before declaring Phase 1 done.

1. **`pytest tests/` passes.** Event log, projection, frontmatter, adapter (mocked) — all green.
2. **The plugin renders.** Installing the built plugin into a test vault makes a "Nexus Chat" view available, and opening it produces a chat-like UI accepting user input.
3. **End-to-end streamed turn.** Typing a message and pressing Enter streams a real response from Sonnet 4.6 (subscription-mode, via Ruflo by default).
4. **Vault persistence.** That turn writes to `Orchestrator/Live/{date}_thread-{id-short8}_{title}.md` with valid YAML frontmatter matching `shared/yaml-schemas.md` (Phase 1 minimum subset).
5. **Event log integrity.** The same turn appears in `Orchestrator/Logs/{id-short8}.jsonl` as `turn_user` followed by `turn_assistant` events.
6. **Restart survival.** Closing the plugin and reopening it shows the prior thread, with all turns intact, restored from the JSONL.
7. **Ruflo-optional.** Setting `use_ruflo: false` in `Config/cascade.yaml` and restarting the orchestrator produces identical user-visible behaviour (the adapter invokes Claude Code directly).

## Ticket order and dependencies

Tickets are ordered so each one builds only on what's already merged. Do not work them out of order.

```
P1-T01 (repo scaffolding)
   ↓
P1-T02 (core types + config loading)
   ↓
   ├─→ P1-T03 (event log)
   │      ↓
   │   P1-T04 (markdown projection)
   │
   └─→ P1-T05 (Anthropic adapter)
              ↓
           P1-T06 (orchestrator bridge — wires T03, T04, T05)
              ↓
           P1-T07 (Obsidian plugin chat view)
              ↓
           P1-T08 (end-to-end acceptance)
```

| Ticket | Roughly one session? | Blocks |
|---|---|---|
| P1-T01 — Repo scaffolding | Yes, small | T02+ |
| P1-T02 — Core types + config | Yes | T03, T05 |
| P1-T03 — Event log | Yes | T04, T06 |
| P1-T04 — Markdown projection | Yes | T06 |
| P1-T05 — Anthropic adapter | Yes, the big one | T06 |
| P1-T06 — Orchestrator bridge | Yes | T07 |
| P1-T07 — Obsidian plugin chat view | Yes | T08 |
| P1-T08 — Integration + acceptance | Yes | — |

## What Phase 1 deliberately does *not* include

The following are explicitly out of scope for Phase 1 and have their own tickets in later phases. **Don't sneak them in.**

- Other adapters (Google, OpenRouter, Ollama). Phase 2.
- Trigger taxonomy + cascade engine. Phase 2.
- Handover document generation. Phase 2.
- SQLite / FAISS / BM25 retrieval. Phase 3.
- Cost dashboard, quota indicator, cascade visualiser. Phase 4.
- Opus button or thread pinning. Phase 4.
- Conflict resolution UI. Phase 4.
- Onboarding wizard. Phase 5.

Phase 1 is the spine. Get the spine right before adding limbs.
