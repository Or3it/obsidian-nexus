# Obsidian Nexus — Claude Code Working Context

You are building **The Obsidian Nexus**: a single-user, locally-run AI orchestration layer that uses an Obsidian vault as the authoritative memory layer across multiple AI providers, with failover cascade and three-tier data sorting.

The full PRD is `obsidian-nexus-technical-plan.md` in the project root. Don't re-read it on every ticket — you have a ticket-by-ticket breakdown below. Read the relevant ticket file plus its referenced `shared/` docs, then build.

---

## How this build is structured

```
nexus-build/
├── CLAUDE.md                       ← you are here
├── shared/                         ← cross-cutting specs, read as referenced by tickets
│   ├── data-architecture.md        ← vault folder layout
│   ├── yaml-schemas.md             ← conversation & handover frontmatter
│   ├── file-conventions.md         ← filename rules, IDs, paths
│   ├── adapter-protocol.md         ← the Adapter interface contract
│   └── risks-to-verify.md          ← facts in the PRD that need real-time verification
├── phase-1/
│   ├── 00-OVERVIEW.md              ← Phase 1 goal + ticket list + global acceptance
│   ├── P1-T01-repo-scaffolding.md
│   ├── P1-T02-core-types-and-config.md
│   ├── P1-T03-event-log.md
│   ├── P1-T04-markdown-projection.md
│   ├── P1-T05-anthropic-adapter.md
│   ├── P1-T06-orchestrator-bridge.md
│   ├── P1-T07-obsidian-plugin.md
│   └── P1-T08-integration-and-acceptance.md
├── phase-2/                        ← (not yet generated)
├── phase-3/                        ← (not yet generated)
├── phase-4/                        ← (not yet generated)
└── phase-5/                        ← (not yet generated)
```

Each ticket file declares: **Goal, Files to create, Dependencies on prior tickets, Implementation notes, Tests to write, Definition of done.**

---

## Universal rules for every ticket

1. **One ticket = one Claude Code session.** Don't try to do P1-T02 and P1-T03 in the same run unless the ticket explicitly says to combine them.
2. **Don't invent facts you can verify.** Several claims in the PRD are time-sensitive (Ruflo's latest npm version, current OpenRouter free-tier model slugs, exact Claude Code CLI flag names). When a ticket says "verify at build time," actually run `npm view`, `curl`, or `claude --help` and use what you find. See `shared/risks-to-verify.md`.
3. **The vault is the source of truth.** Anywhere the orchestrator needs durable state, write to the vault first, then update in-memory state. Never the reverse.
4. **Append-only, then project.** Conversations are stored as JSONL event logs. Markdown files are projections of the JSONL, not the source. If markdown and JSONL disagree, JSONL wins and markdown is rebuilt.
5. **Single writer per `/Orchestrator/`.** The orchestrator is the only process that writes to `Live/`, `Archive/`, `Handovers/`, `Index/`, `Logs/`, `Conflicts/`. Other tools (Obsidian itself, git, Obsidian Sync) must be excluded.
6. **No telemetry. Ever.** No analytics, no error reporting, no "anonymous usage stats." Keys via OS keychain, never in vault files.
7. **Hot-reloadable YAML configs.** `Config/cascade.yaml`, `Config/budgets.yaml`, `Config/style.yaml` — watch for changes and pick them up without restart. Validate on load; refuse to apply invalid configs (log error, keep last good).
8. **Tokenizer drift is real.** Every adapter exposes its own `count_tokens()`. Never use a generic estimator across adapters.
9. **Tests first or alongside, not after.** Phase 1 acceptance includes `pytest tests/` passing. Don't merge a ticket without its tests green.
10. **`OPEN_QUESTIONS.md` is append-only.** Never delete or rewrite existing entries — only add new ones. Drift discoveries, deferred decisions, and known gaps accumulate there as the build's living memory. If you think an entry is outdated, append a new entry referencing it, never edit it. The user reviews this file to understand what the build agents have learned the hard way.

---

## Phase 1 scope reminder

Phase 1 implements **only the Sonnet 4.6 default path** at Rung 1. **Opus 4.7 routing, Haiku 4.5 summariser invocation, and the `use_ruflo: false` fallback are all valid Phase 1 scope** (they're the same adapter, different config); but the *cascade engine* that routes between rungs, the *trigger taxonomy*, and *handover document generation* are Phase 2. Don't sneak them in.

---

## What is out of scope for V1 (do not build these)

- Multi-user / shared vaults.
- Mobile (Obsidian iOS/Android).
- Voice / audio I/O.
- Image generation.
- Binary attachment (PDF/image/audio) indexing.
- Fine-tuning or custom adapters.
- Auto-escalation heuristic for Opus (V1 ships explicit-user-only).
- API spend without explicit opt-in.
- Plugin marketplace listing on day one.

---

## Tech stack (locked for V1)

- **Backend orchestrator:** Python 3.11+, Pydantic 2.6+, `httpx`, `pyyaml`, `python-frontmatter`, `tenacity`, `pytest`, `respx`.
- **Frontend plugin:** TypeScript 5+, Obsidian plugin scaffold (`obsidian-sample-plugin` template), esbuild.
- **Storage:** Plain markdown + JSONL in vault. SQLite (`threads.sqlite`) + FAISS (`embeddings.faiss`) + in-memory BM25 in Phase 3.
- **Embeddings:** Local Ollama (`nomic-embed-text`, 768-dim default). No sentence-transformers.
- **Auth (Rung 1):** Claude Code via Ruflo subprocess wrapper via the user's Claude Pro subscription. Direct-Claude-Code fallback via `use_ruflo: false` config.

---

## Convention signals

- **Commit messages:** conventional commits (`feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`).
- **Python:** type hints required, `ruff` + `mypy` clean.
- **TypeScript:** strict mode, no `any` without justification comment.
- **Branch per ticket:** `phase-1/p1-t03-event-log` etc.
- **Don't `git push`** from inside Claude Code unless the ticket explicitly says to. Commit locally; the user pushes.

---

## When you're unsure

- If a ticket references an external fact you don't have current knowledge of, **verify it** (`npm`, the docs at https://docs.claude.com/en/docs/claude-code/, etc.) — don't guess from memory.
- If two parts of a ticket seem to contradict, the `shared/` spec wins over the ticket prose. The ticket prose is interpretation; the shared spec is contract.
- If a feature isn't specified, don't add it. Note the gap in `OPEN_QUESTIONS.md` at repo root and continue with the smallest reasonable behavior.
