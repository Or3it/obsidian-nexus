# Phase 2a — Failover Cascade

## Goal

Make the Nexus survive a Rung 1 failure. When Sonnet 4.6 hits its weekly Pro quota, returns a refusal, or Anthropic has an outage, the orchestrator detects it, generates a handover document via Haiku, and continues the conversation on Gemini 3.1 Pro (Rung 2). If Gemini fails, fall through to GLM-5.1 → Qwen3-Coder/DeepSeek R1 → local Ollama.

**What "done" looks like:** force a Rung 1 failure (manually, by editing config to point at a bad model string, or by hitting Pro quota), watch the orchestrator generate a handover doc, see the chat continue on Rung 2 with a UI marker, see the prior context preserved.

No web tools. No Opus routing logic. No vault-note integration. Those are Phase 2b. Phase 2a is *failover*.

## Phase 2a global acceptance criteria

1. **`pytest tests/` passes.** All Phase 1 tests stay green; ~50 new tests across adapters, cascade, handover, triggers.
2. **All five adapters implement the protocol.** Anthropic (extended for Opus/Haiku), Google, OpenRouter, Ollama. Each has unit tests against mocked endpoints.
3. **Trigger taxonomy fires correctly.** Tests cover each of the seven trigger classes producing the expected failover or retry behaviour.
4. **Handover docs generate.** When Rung 1 fails over, a handover doc appears in `Orchestrator/Handovers/` with all six required fields populated by Haiku.
5. **Continuation preserves context.** The next model in the cascade receives the handover and continues the conversation coherently.
6. **Hot-reload works.** Editing `Config/cascade.yaml` and saving picks up changes without restart.
7. **UI shows the active rung.** A small badge in the chat header reflects which model is currently answering. Updates in real time when a failover happens.
8. **Live test against real providers passes.** With Google AI Studio key and OpenRouter key configured, manually triggered failover from Rung 1 → Rung 2 → Rung 3 produces clean streamed responses on each.

## Ticket order and dependencies

```
P2a-T01 (shared spec updates: trigger taxonomy, cascade contract)
   ↓
P2a-T02 (Google adapter — Gemini 3.1 Pro free tier)
   ↓
P2a-T03 (OpenRouter adapter — GLM, Qwen, DeepSeek)
   ↓
P2a-T04 (Ollama adapter — local)
   ↓
P2a-T05 (Anthropic adapter extensions — Opus + Haiku routing)
   ↓
P2a-T06 (trigger taxonomy + detector)
   ↓
P2a-T07 (Haiku summariser + handover generation)
   ↓
P2a-T08 (cascade engine — rung selection, failover, hot-reload)
   ↓
P2a-T09 (orchestrator + server wiring for cascade)
   ↓
P2a-T10 (plugin UI — active-rung badge, failover toast)
   ↓
P2a-T11 (integration + acceptance)
```

| Ticket | Roughly one session? | Blocks |
|---|---|---|
| P2a-T01 — shared spec updates | Small | T02–T11 |
| P2a-T02 — Google adapter | Yes | T03+ |
| P2a-T03 — OpenRouter adapter | Yes | T04+ |
| P2a-T04 — Ollama adapter | Yes (small) | T05+ |
| P2a-T05 — Anthropic Opus+Haiku | Yes | T07+ |
| P2a-T06 — trigger taxonomy | Yes | T08+ |
| P2a-T07 — Haiku summariser + handover | Yes | T08+ |
| P2a-T08 — cascade engine | Yes, the biggest | T09+ |
| P2a-T09 — orchestrator wiring | Yes | T10+ |
| P2a-T10 — plugin UI badge + toast | Yes (small) | T11 |
| P2a-T11 — integration + acceptance | Yes | — |

## What Phase 2a deliberately does *not* include

- Web tools (search, fetch). Phase 2b.
- Opus auto-escalation heuristic. Phase 2b (only manual `#opus` tag and button).
- "Continue from vault note" command. Phase 2c.
- SQLite/FAISS/BM25 retrieval. Phase 3.
- Cost dashboard, quota indicator, cascade visualiser. Phase 4.
- Conflict resolution UI. Phase 4.

## Pre-flight checklist

Before P2a-T01:

- [ ] Google AI Studio API key generated at https://aistudio.google.com/apikey
- [ ] OpenRouter API key generated at https://openrouter.ai/keys
- [ ] Ollama running locally (`ollama serve` or via LaunchAgent) with at least one chat-capable model pulled
- [ ] Phase 1 is committed and the orchestrator is running via the LaunchAgent
- [ ] OPEN_QUESTIONS.md has the Phase 1 CORS finding plus the four Phase 2/3 feature-request entries
