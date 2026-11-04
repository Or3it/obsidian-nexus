# Data Architecture (Vault Structure)

**Status:** Contract. Tickets reference this; do not redefine it inside tickets.

## Folder layout

The Nexus owns a single top-level folder inside the user's vault: `Orchestrator/`. The user does not edit anything under it. A sentinel file at the root of that folder signals the convention to humans.

```
{Vault Root}/
└── Orchestrator/                  ← Nexus-managed; user does not edit
    ├── _DO_NOT_EDIT.md             ← convention signal (sentinel file)
    ├── Live/                       ← active conversations
    │   └── 2026-05-13_thread-a1b2c3d4_designing-cascade.md
    ├── Archive/                    ← completed conversations, by month
    │   └── 2026/05/
    │       └── 2026-05-10_thread-e5f6g7h8_old-thread.md
    ├── Handovers/                  ← structured handover docs
    │   └── a1b2c3d4_handover_01.md
    ├── Index/                      ← derived indices (Phase 3+)
    │   ├── threads.sqlite
    │   ├── embeddings.faiss
    │   └── index.md                ← human-readable Dataview index
    ├── Logs/                       ← append-only event logs (JSONL) + daily diagnostic logs
    │   ├── a1b2c3d4.jsonl          ← per-thread event log
    │   └── orchestrator-2026-05-13.log
    ├── Conflicts/                  ← concurrency overflow (Phase 4+)
    └── Config/
        ├── cascade.yaml            ← hot-reloadable cascade definition
        ├── budgets.yaml            ← Pro quota tracking + API caps
        ├── style.yaml              ← style-profile defaults
        ├── tokenizer_ratios.yaml   ← chars-per-token by model
        └── ruflo.lock              ← pinned Ruflo version
```

## Phase availability

- **Phase 1** creates: `Live/`, `Logs/`, `Config/`, `_DO_NOT_EDIT.md`.
- **Phase 2** adds: `Handovers/`, `Archive/`.
- **Phase 3** adds: `Index/`.
- **Phase 4** adds: `Conflicts/`.

Phase 1 should still create empty `Handovers/`, `Archive/`, `Index/`, `Conflicts/` directories with `.gitkeep` so the layout is fully present from day one.

## Writer rules

- **Only the orchestrator writes** to anything under `Orchestrator/`.
- **JSONL is canonical**, markdown is projected. If they disagree, JSONL wins; rebuild markdown.
- **Atomic appends only** to JSONL (use `O_APPEND` / equivalent).
- On startup, if a `Live/*.md` file's projection doesn't match its JSONL source, rebuild the markdown and log a warning.
- If a `Logs/{thread-id}.jsonl` file is corrupt mid-line, truncate to the last valid newline and log a recovery event.

## External-tool exclusion

The Phase 5 onboarding wizard adds `Orchestrator/` to `.gitignore` if git is detected and offers Obsidian Sync selective-sync exclusion. Phase 1 only needs to document this in the README; no runtime enforcement required until Phase 5.

## ID conventions

See `file-conventions.md`.
