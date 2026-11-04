# The Obsidian Nexus

**Technical Plan, PRD, and Build Specification**
Prepared: 13 May 2026
Author role: Senior AI Solutions Architect / Lead Product Designer
**Reader: Claude Code (build agent)** — this document is an executable specification. Where reasoning is given, it is to inform Claude Code's design decisions during implementation, not as background prose.
Status: Ready for Phase 1 execution.

---

## Executive Framing

The Obsidian Nexus is a single-user, locally-run AI orchestration layer that treats an Obsidian vault as the authoritative memory layer for all model interactions. Four operating parameters: failover cascade across providers, vault-as-memory, three-tier data sorting, and portfolio-grade UX. The deeper truth underneath those parameters is that *the orchestrator's value rests on whether handovers between models feel continuous to the user, and whether the vault can be trusted as ground truth across concurrent writes*. Everything in this document subordinates to those two truths.

The existing Obsidian AI plugin landscape (Smart Connections, Copilot for Obsidian, Text Generator, Local GPT) is acknowledged only as positioning: those plugins solve retrieval against notes and single-model chat. None solve persistent, multi-model state with transparent failover and the vault as canonical memory. That gap is the product.

The user is on Claude Pro ($20/month) and intends to use the free tiers of every other rung during initial testing. The cascade is architected to make this a first-class operating mode, not a downgraded version of a paid system.

---

## Deliverable 1 — The Failover Cascade

### Reasoning behind the cascade

A failover cascade is a graceful-degradation curve. Three properties trade against each other:

1. **Capability** — does the next model produce work the user would accept?
2. **Provider independence** — same-provider fallbacks collapse together during outages or shared rate limits. Real failover cascades cross provider boundaries.
3. **Sovereignty floor** — the bottom rung must run with no external dependency. Network down, all APIs throttled, billing card declined — the system still answers. That means Ollama at the bottom.

The user is paying for Claude Pro. The orchestrator monetises that by routing the Anthropic adapter through Claude Code (which is covered by Pro), not through the pay-per-token API. Inside Rung 1 there is an internal hierarchy of three Claude models, each chosen to preserve weekly Pro quota for the cases that actually need it.

### Rung 1 — Claude via Pro Subscription (Sonnet default, Opus on-demand, Haiku summariser)

**Provider:** Anthropic, authenticated via Claude Code via Ruflo, using the user's Pro subscription. No per-token API charges while inside the Pro weekly quota.

Rung 1 hosts three distinct Claude models, each with a defined role:

**Default model — Claude Sonnet 4.6.** Every Rung 1 turn defaults here. Sonnet 4.6 holds 87.6% SWE-bench Verified, near-Opus on most tasks, and burns Pro quota roughly 3–5× slower than Opus per turn. This is the model that should handle 90%+ of conversational work. Pricing inside Pro: covered by subscription. Out-of-band (API): $3 input / $15 output per 1M tokens.

**Premium on-demand model — Claude Opus 4.7.** Released 16 April 2026, leads SWE-bench Pro at 64.3% and offers 128K max output. Used only when explicitly invoked by the user (per-turn button or thread pin) or when a heuristic flags the incoming turn as high-complexity (long context, multi-file reasoning, structured generation over 4K tokens). 1M-token context. The new Opus 4.7 tokenizer produces 1.0–1.35× more tokens for the same raw text than 4.6, so quota-cost-per-turn is higher than the sticker comparison suggests. Out-of-band (API): $5 input / $25 output per 1M tokens.

**Summariser model — Claude Haiku 4.5.** Invisible to the user. Generates handover documents at failover boundaries, scores retrieval candidates, infers thread titles and tags. Haiku barely touches the Pro quota and is the right tool for high-frequency low-stakes calls. Out-of-band (API): $1 input / $5 output per 1M tokens.

**Authentication chain.** Pro → Claude Code → Ruflo subprocess wrapper → Nexus orchestrator's Anthropic adapter. The April 4, 2026 Anthropic policy blocking third-party tools from subscription auth applies to apps authenticating directly to the Anthropic API with subscription credentials. Ruflo wraps Claude Code (which does its own auth), so this path is unaffected.

**Pro weekly limits.** Pro added weekly limits in August 2025 to stop 24/7 background usage. Opus burns the quota faster than Sonnet. On May 6, 2026 Anthropic doubled the 5-hour rate limits on Pro and removed peak-hour throttling, but the weekly cap still exists. The orchestrator must track quota consumption and surface a quota indicator in the UI; when quota exhausts, the cascade auto-fails to Rung 2.

**Cascade-relevant quirks for Claude Code to know:**
- Ruflo's CLI surface is mid-v3 rewrite and unstable; pin to a specific version in `Config/cascade.yaml` and ship a `use_ruflo: false` fallback path that invokes Claude Code directly without the wrapper.
- Claude Code's output is line-oriented JSON when invoked with the right flags; the adapter parses this into the Nexus's standard response shape.
- Subscription-mode invocations do not return token counts in the same shape as API responses. The adapter must approximate token usage from response length and update the quota ledger accordingly.

### Rung 2 — Gemini 3.1 Pro (Google AI Studio free tier)

**Provider:** Google AI Studio. Free tier, no credit card required at signup, generous quota (~15 requests per minute, ~1500 per day on Gemini 3.1 Pro at time of writing — verify current limits at runtime).

**Why position 2:** First non-Anthropic provider in the chain, which is the provider-diversification rung. 94.3% GPQA Diamond — the top reasoning score among frontier models. 1M-token context with caching. Multimodal input native (text, image, audio, video). Output style differs from Claude's prose-default toward more structured output, which the style normalisation layer in Deliverable 2 mitigates.

**Why free works here:** Google AI Studio's Gemini free tier is the cleanest free path in the cascade — official, well-documented, real frontier quality. The rate limit (15 RPM) is higher than what a single user generates in normal conversation, so it rarely blocks. Daily quota covers a heavy day of use.

**Failure modes:** Gemini "thinking" mode hallucinates more (10%+ on Vectara) than Flash variants; the orchestrator should set thinking level to `low` or `medium` for Rung 2 conversational turns and reserve high-thinking for explicit user-flagged hard tasks.

**Auth/hosting:** Google AI Studio API key, free at aistudio.google.com.

### Rung 3 — GLM-5.1 (OpenRouter free tier)

**Provider:** OpenRouter free tier (`zhipu/glm-5.1:free` or current equivalent model slug). Authenticated with a free OpenRouter API key.

**Why position 3:** Third provider in the chain (Zhipu AI via OpenRouter as gateway), MIT-licensed open weights, briefly held #1 on SWE-bench Pro in April 2026 — the first open-weight model ever to top that board. Strong reasoning, competitive coding, fully free at this tier.

**Failure modes:** GLM-5.1 had real-world consistency issues in the May 2026 Rails+RubyLLM benchmark (invented DSL syntax, dropped history) that contradict its SWE-bench Pro showing. The orchestrator should flag GLM-5.1 turns with a "verify-before-trust" marker in the UI — small, non-intrusive — so the user is reminded to spot-check rather than auto-trust the output.

**OpenRouter free-tier rate limits:** ~20 requests per minute, ~200 per day, shared across *all* free OpenRouter models on a given API key. Rungs 3 and 4 share this pool, which is a real constraint and must be accounted for in the cascade logic.

**Auth/hosting:** OpenRouter API key (free at openrouter.ai/keys, no card required).

### Rung 4 — Qwen3-Coder-480B / DeepSeek R1 (OpenRouter free tier)

**Provider:** OpenRouter free tier. Two model choices behind a single rung, selected by use-case:
- `qwen/qwen3-coder-480b:free` — strongest free coding model, 262K context.
- `deepseek/deepseek-r1:free` — strongest free reasoning model.

**Why position 4:** Provider-diverse fourth rung (Alibaba and DeepSeek, both routed through OpenRouter as gateway). Both are real frontier-adjacent quality and both are free. The dual-model option lets the orchestrator pick by intent — code-heavy turn → Qwen3-Coder, reasoning-heavy turn → DeepSeek R1.

**Shared rate limit with Rung 3.** Rungs 3 and 4 both consume the OpenRouter free pool. The cascade trigger logic must treat hitting the OpenRouter daily quota as a single event that fails *both* rungs over to Rung 5 simultaneously.

**Failure modes:** Free OpenRouter models can be silently removed or have limits changed without notice. The orchestrator should hold a list of fallback model slugs per logical rung-position and try them in order before declaring the rung dead.

**Auth/hosting:** OpenRouter API key (same key as Rung 3).

### Rung 5 — Local Ollama (hardware-aware model picker)

**Provider:** Local Ollama at `localhost:11434`. No API key, no rate limit, no network dependency.

**Why position 5:** Sovereignty floor. Network down, all APIs throttled, OpenRouter daily quota burned, Pro week reset hours away — the system still answers, on the user's own hardware.

**Hardware-aware picker.** Rung 5 is not a single model. At first run, the onboarding flow detects available VRAM and recommends the appropriate model. The user can override. Recommendations:

| VRAM | Recommended primary | Strong alternative |
|---|---|---|
| 8 GB | Llama 4 Scout 17B (Q4) | Hermes 4 8B (Q4) |
| 16–24 GB | Qwen3-30B-A3B | Hermes 4 70B (Q4) or Gemma 4 26B |
| 24–32 GB | Qwen3.6 35B | Gemma 4 31B or GLM-5.1 (heavy quant) |
| 32 GB+ | Hermes 4 405B (Q4) | Llama 4 Maverick |

**Why Hermes 4 deserves a slot:** Nous Research's Hermes 4 (open-weight, four sizes: 3B/8B/70B/405B) offers hybrid reasoning mode that takes MATH-500 from 93.1% to 96.3% on the 405B tier, and is designed around user-alignment with reduced refusal patterns. For a single-user system handling personal or sensitive material, that posture matters.

**Failure modes:** Cold-start latency when the model unloads from VRAM. Set `keep_alive: -1` for the active local model. Smaller effective context window than cloud rungs (32K–128K practical, depending on quant). The orchestrator marks threads continued on Rung 5 with a `local_degraded` flag so the user can re-promote them on next session.

**Auth/hosting:** Local Ollama, no key. Onboarding installs Ollama if absent and pulls the recommended model.

### Cascade summary

| # | Model | Provider | Auth | Cost | Context |
|---|---|---|---|---|---|
| 1 | Claude Sonnet 4.6 (default) + Opus 4.7 (on-demand) + Haiku 4.5 (summariser) | Anthropic via Claude Code via Ruflo | Pro subscription | Covered by Pro | 200K / 1M / 200K |
| 2 | Gemini 3.1 Pro | Google AI Studio | Free API key | Free | 1M (with caching) |
| 3 | GLM-5.1 | Zhipu via OpenRouter | OpenRouter free key | Free | 128K |
| 4 | Qwen3-Coder-480B / DeepSeek R1 | Alibaba / DeepSeek via OpenRouter | OpenRouter free key (shared with R3) | Free | 262K / 128K |
| 5 | Hardware-detected: Hermes 4 / Qwen3 / Gemma 4 / Llama 4 / GLM-5.1 | Local | Local Ollama | Free (compute only) | 32K–128K practical |

### Risks and Open Questions — Deliverable 1

- **Tokenizer drift between rungs.** Each adapter must expose its own `count_tokens(text)` method. The orchestrator's context-packing tier budgets are computed against the active adapter's tokenizer, never against a generic estimate.
- **Pro quota tracking is approximate.** Subscription-mode Claude Code does not return per-turn token counts in the same shape as API responses. The adapter must estimate from response length and call frequency, and accept that the indicator in the UI is "best-effort" not "to-the-token accurate."
- **Ruflo coupling is now load-bearing.** With Pro-subscription auth routed through Ruflo, a Ruflo break cascades into total loss of Rung 1 free access (forcing API spend or skip-to-Rung-2). Mitigation: the `use_ruflo: false` fallback path invokes Claude Code directly, accepting the loss of cache-aware token tracking but preserving subscription access. Pin Ruflo to a specific version in `Config/cascade.yaml`.
- **OpenRouter free-tier ToS drift.** OpenRouter has stated free-tier availability is not guaranteed. The orchestrator should treat Rungs 3 and 4 model slugs as configurable and ship with at least two candidate slugs per logical position.
- **Model deprecation cadence.** Frontier models version-bump every 4–10 weeks. Cascade config must be hot-reloadable YAML, not hard-coded.
- **Tool-use sub-cascade not in V1.** Different rungs support different tool-use schemas. V1 cascade is linear; capability-based branching deferred to V2.

---

## Deliverable 2 — Logic Stress-Test

### 1. Context loss during mid-conversation handover

**The hole.** When the active model is exhausted and the next one takes over, the new model has no working memory of the conversation. Naive transcript replay either exceeds context limits on long threads or buries current intent under noise.

**The solution.** A two-tier handover with the orchestrator as trusted authority. The orchestrator generates a structured handover document at every model boundary using the Haiku 4.5 summariser. Required fields:

| Field | Content |
|---|---|
| `thread_id` | UUID v7 of the conversation. |
| `summary` | ≤ 500 words: what is being worked on, what has been concluded, what is contested, what is the user's apparent goal. |
| `open_questions` | Explicit list of unresolved questions. |
| `last_user_intent` | One sentence describing the user's most recent ask, in their words where possible. |
| `recommended_model` | Next rung selected, with one-line rationale. |
| `full_history_path` | Wiki-link to the live conversation file. |

**Why the orchestrator, not the outgoing model.** The reason for failover is often that the outgoing model is unavailable. You cannot rely on an unresponsive model to write its own exit doc. The orchestrator uses Haiku 4.5 (or the local Ollama model if Pro is exhausted) for summariser duties; this model never appears in the user-facing cascade.

**Optimisation.** If the outgoing model is *still responsive but soon-exhausted* (e.g., Sonnet has answered the current turn but quota will hit on the next), the orchestrator can pre-emptively ask it to write its own handover. The orchestrator-generated handover is the fallback.

### 2. Context window overflow

**The hole.** Live history + relevant archive + relevant vault notes + system prompt + current turn can together exceed any model's context window.

**The solution.** Tiered context packing with deterministic priority. Each tier has a token budget computed from the active model's context window:

```
total_window = model.context_window
reserved_for_response = max_output_tokens (model-specific)
reserved_for_system = ~2K
available = total_window - reserved_for_response - reserved_for_system

tier_1 (live tail):           up to 40% of available
tier_2 (compressed history):  up to 10% of available
tier_3 (archive retrieval):   up to 25% of available
tier_4 (vault retrieval):     up to 25% of available
```

Surplus rolls down. Tiers can run under-budget; nothing requires filling the window.

### 3. Model behaviour divergence

**The hole.** Sonnet/Opus write prose. Gemini prefers structured output. GLM and Qwen run terser. The texture change on handover looks like a system bug to the user even when it isn't.

**The solution.** A thread-level style profile (`format`, `verbosity`, `posture`, code-block conventions) prepended to every system prompt by every adapter. The orchestrator does not try to make Model B sound like Model A; it anchors the *user experience* across handovers.

Plus a UI affordance for honesty: at every model handover, a one-line marker is inserted in the conversation file (and shown in the chat view as a subtle divider). Users tolerate handovers when they're explicit; they distrust the system when texture changes unexpectedly.

### 4. Failover trigger taxonomy

**The hole.** "Rate limit hit" is the easy case. Slow responses, partial completions, refusals, and quota walls all need named handling.

**The solution.** A seven-class trigger taxonomy. (One class is new versus the prior draft, to handle subscription-quota exhaustion as a first-class event.)

| Trigger class | Detector | Default threshold | Action |
|---|---|---|---|
| `HARD_FAIL` | HTTP 401/403/429 with no retry-after, or 5xx after 2 retries | Immediate | Failover, no retry |
| `PRO_QUOTA_EXHAUSTED` | Claude Code reports subscription weekly cap, or adapter detects 60-second hangs on subscription calls | 1 confirmed event | Failover to Rung 2; mark thread with `pro_quota_exhausted_until: {next_reset_timestamp}` |
| `SOFT_DEGRADE` | Streaming latency > 3× rolling P95 baseline for 2 consecutive requests | After 2 consecutive | Failover with toast notification |
| `OPENROUTER_QUOTA` | OpenRouter returns daily-quota-exhausted error on free models | Immediate | Skip Rungs 3+4 together; failover to Rung 5 |
| `CONTEXT_LIMIT` | Computed prompt size > 90% of active model's window | Per-request | Failover to larger-window rung |
| `EXPLICIT_REFUSAL` | Model returns policy refusal when prior identical-context turns succeeded | 1 retry then failover | Failover with user-visible note |
| `MANUAL` | User-initiated via pin or override | Immediate | Honour user's choice |

Retries are limited. `HARD_FAIL` and `PRO_QUOTA_EXHAUSTED` skip retries entirely. `SOFT_DEGRADE` permits one retry with exponential backoff before failing over.

A failover does not reset the retry counter on the rung above. The system does not loop back to Rung 1 until either (a) the recorded `pro_quota_exhausted_until` time has passed, or (b) the user manually pins back.

### 5. State consistency (vault concurrent writes)

**The hole.** The orchestrator writes live conversation files in real time. Obsidian itself, Obsidian Sync, git, or other tools may also write. Races can corrupt notes or duplicate turns.

**The solution.** Three mechanisms:

1. **Single writer per `/Orchestrator/`.** Only the orchestrator writes to `Live/`, `Archive/`, `Handovers/`. A `_DO_NOT_EDIT.md` sentinel signals the convention to humans.
2. **Append-only event log.** Every turn is first appended atomically to `/Orchestrator/Logs/{thread_id}.jsonl`. The markdown file is projected from the JSONL on each turn. If a write to markdown is interrupted, the next run rebuilds from the JSONL.
3. **Optimistic concurrency.** YAML carries `schema_version` and `turn_count`. Writes increment and compare; on mismatch the orchestrator re-reads, re-applies, and writes again. Unresolvable conflicts go to `/Orchestrator/Conflicts/` and surface in the UI.

Obsidian Sync should exclude `/Orchestrator/` via selective sync. Git users should add `/Orchestrator/` to `.gitignore`. The Phase 1 installer surfaces these instructions.

### 6. Cost runaway

**The hole.** Subscription quota is finite. API spend (when escalated) compounds across failed-over models. Without explicit guard rails the user can blow either budget without realising.

**The solution.** Three-tier guard rails plus a Pro-aware ledger.

**Tier 1 — Pro quota ledger.** Per-thread and weekly aggregate tracking of estimated Pro quota consumption. Quota is approximated from response length and call count, not from a per-token meter (the subscription API doesn't expose one). UI shows quota remaining as a coloured bar in the chat header.

**Tier 2 — Per-thread API cap.** If the user enables API escalation (Rung 1.5), threads carry a `budget_cap_usd` (default $2.00) and `budget_remaining_usd`. At 80% the UI warns; at 100% the cascade collapses to Rung 2 free.

**Tier 3 — Per-day aggregate.** Daily cap across all threads ($20 default). When hit, the cascade collapses to Rungs 2+ and the user is notified.

**The cost dashboard.** Three charts: Pro quota burn-down (rolling 7-day), API spend per thread (if enabled), daily total. This is the operational artifact a portfolio reviewer will look for — treat it like a billing statement, not a fitness tracker.

### 7. Additional holes

**Tokenizer mismatch.** Claude Code subscription mode returns no token count. Adapter approximates by character count → token count using a per-model ratio (Sonnet ~3.8 chars/token, Opus ~3.6, Gemini ~3.5, Qwen/GLM/DeepSeek ~3.9 — calibrate against measured samples at first run).

**Tool-calling schema divergence.** Anthropic, OpenAI, Google, and Ollama-via-OpenAI-compat all carry the same intent (let the model call functions) with different wire formats. V1 *defers mid-flight tool calls on failover boundaries* — complete the round on the original model with retries, fail over only at clean turn boundaries.

**Streaming-mid-response failover.** If the active model is mid-stream and connection drops, partial response has been seen by the user. Failover restarts from the user's last input, not from the partial. The orchestrator buffers in-flight tokens and, on connection loss, finishes the stream from the buffer, then explicitly asks the user "the response was interrupted; would you like the new model to continue from here, or restart?" and waits.

**Sensitive-data jurisdiction.** Rungs 3 and 4 route through OpenRouter to Chinese-origin model providers (Zhipu, Alibaba, DeepSeek). Threads marked `jurisdiction_locked: true` skip these rungs even on failover; if all upper rungs are exhausted, the thread pauses with an explanatory error rather than escaping. V1 ships with this lock available per-thread, default off; user can configure global default.

### Risks and Open Questions — Deliverable 2

- **The Haiku summariser is single-point.** If Haiku is unavailable (rare but possible when Anthropic is having a bad day), handover quality collapses. The fallback summariser is the active Rung 5 model — slower and lower-quality but always available.
- **Style normalisation is a research problem disguised as engineering.** V1 ships with format/verbosity/posture controls. Future work could fine-tune a small local model as a style mirror pass at handover boundaries.
- **Cost ledger trusts approximations.** Pro quota is estimated, not measured. API spend (when active) is measured but provider billing reconciliation happens weekly. V1 displays estimates as estimates.
- **Pro quota exhaustion behaviour.** When the Pro week hits zero, the system auto-cascades to Rung 2 with a toast. The toast must explain (a) what happened, (b) when Pro resets, and (c) how the user can switch to API spend if they want Claude back immediately. The UI must not punish the user for an unavoidable quota event.

---

## Deliverable 3 — Data Architecture (Vault Structure)

### Folder layout

```
{Vault Root}/
├── Orchestrator/                  ← Nexus-managed; user does not edit
│   ├── Live/                      ← Active conversations
│   │   └── 2026-05-13_thread-a1b2c3d4_designing-cascade.md
│   ├── Archive/                   ← Completed conversations, by month
│   │   └── 2026/05/
│   │       └── 2026-05-10_thread-e5f6g7h8_old-thread.md
│   ├── Handovers/                 ← Structured handover docs
│   │   └── a1b2c3d4_handover_01.md
│   ├── Index/                     ← Derived indices
│   │   ├── threads.sqlite
│   │   ├── embeddings.faiss
│   │   └── index.md               ← Human-readable Dataview index
│   ├── Logs/                      ← Event logs (JSONL, append-only)
│   │   └── a1b2c3d4.jsonl
│   ├── Conflicts/                 ← Concurrency overflow
│   └── Config/
│       ├── cascade.yaml           ← Hot-reloadable cascade definition
│       ├── budgets.yaml           ← Pro quota tracking + API caps
│       ├── style.yaml             ← Style-profile defaults
│       └── ruflo.lock             ← Pinned Ruflo version
└── _DO_NOT_EDIT.md                ← Convention signal in Orchestrator/
```

### Conversation frontmatter

```yaml
---
type: conversation
schema_version: 2
thread_id: 0192f3b4-1c20-7890-abcd-ef0123456789
parent_thread: null
title: "Designing the failover cascade"
created: 2026-05-13T14:32:00-05:00
updated: 2026-05-13T15:47:12-05:00
status: live              # live | archived | paused | locked
turn_count: 14
pro_quota_consumed_estimate_pct: 12.5
api_spend_usd: 0.00
api_spend_enabled: false
budget_cap_usd: 2.00
models_used:
  - id: claude-sonnet-4-6
    auth_mode: subscription
    turns: [1, 2, 3, 4, 5, 6, 7, 8]
    estimated_tokens: 18000
  - id: claude-opus-4-7
    auth_mode: subscription
    turns: [9]                    # explicit user escalation for hard turn
    estimated_tokens: 4500
  - id: claude-sonnet-4-6
    auth_mode: subscription
    turns: [10, 11, 12, 13, 14]
    estimated_tokens: 9025
active_model: claude-sonnet-4-6
pinned_model: null                # null | claude-opus-4-7 | etc.
jurisdiction_locked: false
pro_quota_exhausted_until: null   # ISO timestamp when set
style_profile:
  format: prose
  verbosity: detailed
  posture: peer
tags: [project/obsidian-nexus, architecture]
related_threads:
  - "[[2026-05-09_thread-aaa11111_initial-brainstorm]]"
related_notes:
  - "[[Project Brief - Obsidian Nexus]]"
handovers: []
---
```

### Handover frontmatter

```yaml
---
type: handover
schema_version: 2
handover_id: 01923d4a-9c20-7c12-bbcd-ef0123456abc
thread_id: 0192f3b4-1c20-7890-abcd-ef0123456789
sequence: 1
from_model: claude-sonnet-4-6
to_model: gemini-3-1-pro
trigger: PRO_QUOTA_EXHAUSTED
trigger_detail: "Pro weekly quota reached; resets 2026-05-19T00:00:00Z"
triggered_at: 2026-05-13T15:47:12-05:00
generated_by: orchestrator
summariser_model: claude-haiku-4-5
tokens_used: 1850
---

## Summary
{≤ 500 words}

## Open Questions
- {item}

## Last User Intent
{one sentence}

## Recommended Continuation Style
{one paragraph}

## Full History Path
[[2026-05-13_thread-a1b2c3d4_designing-cascade]]
```

### Filename conventions

- **Live:** `{YYYY-MM-DD}_thread-{id-short8}_{kebab-title}.md`
- **Archive:** same name, under `/Archive/{YYYY}/{MM}/`
- **Handover:** `{thread-id-short8}_handover_{sequence}.md`
- **Event log:** `{thread-id-short8}.jsonl`

`id-short8` = first 8 chars of UUIDv7. Title is generated by Haiku 4.5 from the first user turn, truncated to 64 chars and kebab-cased. User can rename via UI; underlying `thread_id` never changes.

### Cross-linking, indexing, retrieval

Three indices, three different queries:

- **SQLite (`threads.sqlite`)** — structured filters: date, model, cost, tags.
- **FAISS (`embeddings.faiss`)** — semantic similarity. Embeddings via Ollama local model (`nomic-embed-text` or `bge-m3`, 768-dim).
- **BM25 (in-memory or via Tantivy)** — lexical / keyword overlap.

The retrieval pipeline:

1. User turn → query.
2. BM25 → top 50 candidates.
3. FAISS → top 50 candidates.
4. Reciprocal Rank Fusion → ~80 unique.
5. Filter > 180 days old unless in top 10; filter `archived_excluded: true`.
6. Re-rank top 20 via Haiku 4.5 scoring pass.
7. Pack top-*k* into tier 3 + tier 4 budgets.

### Context-packing prompt template

```
You are an assistant working inside {USER}'s Obsidian Nexus. The user's vault is
the source of truth. Maintain continuity with prior model turns; if you cannot,
say so and ask.

Style profile for this thread:
  format: {prose | structured | mixed}
  verbosity: {concise | default | detailed}
  posture: {assistant | peer | tutor}

──── HANDOVER BRIEF ────
{handover.summary}
Open questions: {handover.open_questions}
Last user intent: {handover.last_user_intent}
Why you are answering this turn: {handover.recommended_model_rationale}

──── ARCHIVED CONTEXT ────
{for each archived item:
  ### {item.title} ({item.date})
  {item.summary_or_excerpt}
  Source: [[{item.path}]]
}

──── LIVE CONVERSATION HISTORY ────
{last_n_turns formatted as standard chat history}

──── CURRENT USER TURN ────
{user_message}

Respond now. If the handover summary contradicts the live history, trust the
live history. If you do not have enough context, ask one specific question
rather than guessing.
```

### Risks and Open Questions — Deliverable 3

- **Index drift.** Indices can fall out of sync with markdown. Phase 3 ships `nexus rebuild-indices` and a startup integrity check.
- **Tag taxonomy ownership.** Orchestrator suggests tags via Haiku 4.5; user has final authority. Tag changes propagate to the index.
- **Vault data scope.** V1 retrieves from text-content notes only. Binary attachments (PDFs, images, audio) are out of scope for retrieval — wiki-link only.
- **Embedding model versioning.** Each embedding is stamped with model ID + version. Mismatch triggers background re-embed.

---

## Deliverable 4 — Product Requirements Document

### 1. Problem statement

Existing Obsidian AI plugins solve retrieval against notes and single-model chat. None solve multi-model orchestration with the vault as durable shared memory. Heavy AI users hit rate limits, quota walls, or capability ceilings mid-conversation; today's plugins either stall or force a manual model switch that loses working state. The Obsidian Nexus treats the vault as durable substrate, the orchestrator as routing intelligence, and individual models as interchangeable execution engines.

### 2. Target user

A single technical knowledge worker who already uses Obsidian, pays for Claude Pro, has free-tier accounts on Google AI Studio and OpenRouter, can run a local model via Ollama, and cares about owning their data. Today they bounce between web chat interfaces and their vault, copy-pasting context and rebuilding lost state when sessions expire. The Nexus eliminates that copy-paste tax.

### 3. Success metrics

| Metric | Definition | V1 target |
|---|---|---|
| **Failover-to-first-token** | Time from active-model failure detection to next-model first streamed token | ≤ 4 seconds (P95) |
| **Context preservation rate** | % of failed-over conversations where the next model correctly references state from before the failover | ≥ 90% |
| **Vault write durability** | % of turns where vault state is intact after process restart | 100% |
| **Pro quota predictability** | Mean absolute error between estimated and actual Pro quota consumed | ≤ 15% |
| **Offline availability** | % of turns successfully answered with no internet | 100% on Rung 5 |
| **Cost (testing config)** | Monthly out-of-pocket spend during free-tier testing | $20 (Claude Pro only) |

### 4. Functional requirements

**Orchestrator.** Receives user turns, decides which model to call (default = current rung's primary; override via pinned/escalated state), packs context per tier budget, executes the call, handles failure per trigger taxonomy, writes results to vault, updates indices and ledger.

**Vault layer.** Owns reads and writes to `/Orchestrator/`. Maintains the append-only event log per thread and projects to markdown. Maintains SQLite, FAISS, BM25 indices. Exposes a retrieval API: `retrieve(query, top_k, filters) → list[Item]`.

**Model adapter layer.** One adapter per logical position, supporting multiple providers per adapter where useful:

- **Anthropic adapter:** authenticates via Claude Code via Ruflo by default (subscription mode). Falls back to direct API key (paid mode) if `api_spend_enabled: true` and Pro quota is exhausted. Supports `use_ruflo: false` config to invoke Claude Code directly without the Ruflo wrapper, accepting the loss of cache-aware token tracking but preserving subscription access. Model selection within the adapter: Sonnet 4.6 (default), Opus 4.7 (on-demand, gated by user pin or escalation heuristic), Haiku 4.5 (summariser only, never user-facing).
- **Google AI Studio adapter:** authenticates with free-tier API key. Targets Gemini 3.1 Pro by default.
- **OpenRouter adapter:** authenticates with free-tier API key. Acts as gateway for Rungs 3 and 4. Targets GLM-5.1, Qwen3-Coder-480B, DeepSeek R1 as configured.
- **GMI Cloud adapter (optional gateway):** alternative routing for any Chinese-origin model in Rungs 3–4. Disabled by default; user enables in `Config/cascade.yaml`.
- **Ollama adapter:** local at `localhost:11434`. Model selection via hardware-detected picker at onboarding; user override at any time.

Each adapter exposes: `complete`, `complete_stream`, `count_tokens` (or approximation), `available_context`, `estimated_cost` (or quota cost), `health_check`.

**UI layer.** Obsidian plugin in TypeScript. Provides chat view, active-model indicator with internal Claude-tier indicator, cascade visualiser, cost/quota dashboard, thread browser, manual override controls (including per-turn Opus button and thread pin).

**Configuration.** YAML files in `/Orchestrator/Config/`. Hot-reloaded on file change.

### 5. Non-functional requirements

- **Performance.** First-token latency on Rung 1 P95 ≤ 3 seconds for prompts under 50K tokens. Local rung first-token ≤ 8 seconds. Index queries P95 ≤ 200ms. Orchestrator overhead above bare model call ≤ 500ms P95 cold context, ≤ 100ms warm.
- **Security.** API keys via OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service). Never in vault files. Claude Code authentication via standard Claude Code login flow (browser-based OAuth to anthropic.com).
- **Privacy.** No telemetry. No analytics. Local-only embeddings. Vault content sent only as part of explicit model API calls.
- **Offline.** Rung 5 functions with no network. Indices queryable offline.
- **Operating system.** macOS, Linux, Windows (WSL2 for Ruflo). Python 3.11+, Node 18+.

### 6. User flows

**Flow 6.1 — Starting a new conversation.** User opens Nexus chat view, types first turn. Orchestrator retrieves against vault and archives, packs context per tier budget, calls Sonnet 4.6 (default) via Claude Code via Ruflo via Pro. Streams response to chat view; writes to vault via event log. UI header shows: active model badge (Sonnet 4.6, blue, with subtle "S" indicator), per-thread Pro quota cost estimate, cumulative thread context affordance.

**Flow 6.2 — User requests Opus for a hard turn.** User clicks the per-turn "Use Opus" button (or hits keyboard shortcut). Next turn routes to Opus 4.7 instead of Sonnet. Header indicator changes to Opus (blue with "O") for that turn only, reverts to Sonnet after. The conversation YAML records `turns: [n]` under the Opus models_used entry.

**Flow 6.3 — Pro quota exhausted.** Orchestrator sends Sonnet a turn, Claude Code returns a quota-exhausted signal. Trigger taxonomy classifies as `PRO_QUOTA_EXHAUSTED`. Orchestrator:
1. Invokes Haiku (if available) or local Ollama summariser for handover document.
2. Updates YAML: `active_model: gemini-3-1-pro`, appends to `models_used`, sets `pro_quota_exhausted_until: {next_reset}`.
3. Re-packs context using Gemini's tokenizer and context window, includes handover in system prompt.
4. Calls Gemini 3.1 Pro via Google AI Studio.
5. UI shows non-modal toast: *"Pro weekly quota reached. Continuing on Gemini 3.1 Pro (free). Resets {date}. Click to enable API spend for Claude instead."*
6. Streamed response appears with the handover marker in the markdown source.

Target elapsed time from quota detection to first Gemini token: ≤ 4 seconds.

**Flow 6.4 — Retrieving archived context for a new question.** User starts new thread, asks "what did we conclude about the cascade ordering?" Orchestrator detects back-reference, runs retrieval pipeline (BM25 + FAISS + RRF + re-rank), surfaces top 3 archived threads + the project brief, packs into tier 3+4 budgets, calls Sonnet. Sonnet answers, citing source by wiki-link. User clicks link, jumps to source.

### 7. UI/UX requirements

**Chat view.** Standard chat layout. Header carries:
- Editable thread title.
- Active-rung badge (coloured pill): R1 blue, R2 green, R3 teal, R4 orange, R5 grey. R1 badge has internal sub-indicator showing which Claude model (`S`/`O`/`H` for Sonnet/Opus/Haiku — Haiku never appears in active state, only in handover docs).
- Pro quota indicator: small horizontal bar, fills with weekly use; turns amber at 80%, red at 95%.
- Turn count and cost-so-far (Pro quota % consumed for this thread + API spend if enabled).
- Context affordance: "3 notes + 2 threads" with click-to-inspect.

Chat body shows turns. Handover events appear as in-line dividers with a soft tooltip. Streaming renders token-by-token.

**Input field.** Carries:
- Per-turn "Use Opus" button. Single click → next turn uses Opus 4.7, reverts after. Visual confirmation (button stays lit for the turn). Keyboard shortcut: `Cmd/Ctrl+Shift+O`.
- Model picker dropdown (default "auto / cascade"). Options: auto, pin Sonnet, pin Opus, pin Haiku (disabled — summariser only), pin Gemini, pin GLM, pin Qwen, pin DeepSeek, pin Local. Pin = whole-thread override; the cascade is disabled until unpinned.
- Standard send button.

**Cascade visualiser.** Collapsible panel. Vertical chain of five rungs (R1 expandable to show Sonnet/Opus/Haiku internal split). Active rung highlighted; rungs above show status (available / quota-exhausted / unavailable) with timestamps. Hover shows context window, cost-per-1K-tokens (or "Pro covered"), last-error. This is the *trust artifact* — user sees at a glance why the system is where it is.

**Cost/quota dashboard.** Separate pane:
- Pro weekly quota burn-down chart (rolling 7-day, with reset markers).
- Per-thread quota estimates (current month).
- API spend (only visible if API mode enabled).
- Per-model split.

**Thread browser.** Dataview-powered table of all threads (live + archived). Filterable by tag, model, date, quota cost. Rows deep-link to source markdown.

**During failover.** Non-modal toast at top of chat view. Self-dismisses after 6 seconds; remains in the thread's event timeline.

**Manual override / pinning.** Two controls. *Pin* fixes a model for the thread. *Override-once* uses a specific model for the next turn only. Both in the model picker dropdown next to input. Also accessible via Obsidian command palette (`Nexus: Pin to model…`, `Nexus: Use Opus for next turn`).

### 8. Out of scope for V1

- Multi-user / shared vaults.
- Mobile (Obsidian iOS/Android).
- Voice or audio I/O.
- Image generation.
- Binary attachment indexing (PDFs, images as searchable content).
- Fine-tuning or custom adapters.
- Automated handover-quality evaluation.
- Plugin marketplace listing on day one.
- API spend without explicit user opt-in (Rung 1.5 is opt-in, default off).

### 9. Open questions

1. **Opus escalation heuristic.** Beyond explicit user invocation, should the orchestrator *auto-escalate* to Opus on detected high-complexity turns? V1 recommendation: no auto-escalation in initial Phase. Ship explicit-user-control first; revisit in Phase 5.
2. **OpenRouter free-tier model slugs.** Free model availability shifts. V1 ships with `glm-5.1:free`, `qwen3-coder-480b:free`, `deepseek-r1:free` as configured slugs with two fallback alternates each in `cascade.yaml`. Claude Code, during Phase 2 build, must verify current slugs at execution time and update the config accordingly.
3. **Rung 5 model on first install.** Hardware detection picks a default, but the user may prefer a different choice. V1 onboarding offers the detected pick with one-click confirmation; full menu available via `nexus model pick`.

### 10. Phased build plan (summary)

Detailed phase specifications are in the *Build Specifications for Claude Code* section below.

- **Phase 1 — Single-model spine (2–3 weeks).** Anthropic adapter with Claude Code + Ruflo + Pro auth. Sonnet 4.6 only. Event log + markdown projection. Minimal plugin chat view.
- **Phase 2 — Cascade + handovers (2–3 weeks).** Four additional adapters. Trigger taxonomy + failover. Haiku summariser. Hot-reloadable cascade.
- **Phase 3 — Retrieval pipeline (3–4 weeks).** SQLite + FAISS + BM25. Local embeddings. Hybrid retrieval + re-rank.
- **Phase 4 — Operational polish (2–3 weeks).** Opus button, quota indicator, cost dashboard, cascade visualiser, conflict UI.
- **Phase 5 — Hardening and ship (2 weeks).** Onboarding wizard, documentation, soak test.

Total: 11–15 weeks part-time; ~8 weeks full-time.

### Risks and Open Questions — Deliverable 4

- **Phase 3 is the longest and most likely to slip.** Retrieval quality has many tunable knobs. Plan a retrieval-evaluation harness sub-phase if quality is below target.
- **Obsidian plugin API can break.** Minimise dependency surface; pin to API versions; treat upgrades as deliberate sub-projects.
- **Pro quota tracking is approximate.** Estimated, not metered. UI must communicate this so users don't expect to-the-token accuracy.
- **Ruflo as load-bearing dependency.** Re-stated for emphasis. The `use_ruflo: false` fallback path is not optional — it must be implemented in Phase 1 alongside the Ruflo path, so a Ruflo break is recoverable without a build-time fix.

---

## Build Specifications for Claude Code

This section is the executable layer. Each phase below specifies what Claude Code must build, in what order, with what dependencies, and what acceptance criteria verify completion. Where current model slugs, Ruflo versions, or external package names matter, Claude Code is expected to verify against current sources at execution time (especially `cascade.yaml` defaults and Ruflo's npm latest tag).

### Phase 1 — Single-model spine

**Outcome.** A working conversation with Claude Sonnet 4.6 via Claude Code via Ruflo via the user's Pro subscription, persisting to the vault and surviving plugin restart.

**Repository structure to create:**

```
nexus/
├── pyproject.toml
├── README.md
├── orchestrator/
│   ├── __init__.py
│   ├── orchestrator.py            ← top-level entrypoint
│   ├── config.py                  ← YAML loaders
│   ├── types.py                   ← Pydantic models for requests/responses
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py                ← Adapter protocol
│   │   └── anthropic.py           ← Claude Code via Ruflo subprocess
│   └── vault/
│       ├── __init__.py
│       ├── event_log.py           ← JSONL append-only writer
│       ├── projection.py          ← JSONL → markdown projection
│       └── frontmatter.py         ← YAML schema validation
├── plugin/                        ← Obsidian plugin (TypeScript)
│   ├── manifest.json
│   ├── main.ts
│   ├── views/
│   │   └── chat-view.ts
│   ├── styles.css
│   └── package.json
└── tests/
    ├── test_event_log.py
    ├── test_projection.py
    ├── test_anthropic_adapter.py
    └── fixtures/
```

**Python dependencies (`pyproject.toml`):**
- `pydantic >= 2.6`
- `httpx >= 0.27`
- `pyyaml >= 6.0`
- `python-frontmatter >= 1.0`
- `tenacity >= 8.2` (retry logic)
- `pytest >= 8.0` (dev)
- `pytest-asyncio >= 0.23` (dev)
- `respx >= 0.21` (HTTP mocking, dev)

**Node/TypeScript dependencies (plugin):**
- Obsidian plugin scaffold from `obsidian-sample-plugin` template.
- `typescript >= 5.0`
- `esbuild >= 0.20`

**External setup (Phase 1 onboarding script must perform):**
1. Verify Node.js >= 18 is installed.
2. Install Claude Code: `npm install -g @anthropic-ai/claude-code` then prompt user to run `claude /login` and authenticate via Pro account.
3. Install Ruflo at the version pinned in `Config/ruflo.lock`: `npm install -g ruflo@{pinned-version}`. Default pinned version for Phase 1: latest stable on the day of build. Claude Code should query `npm view ruflo version` and pin that.
4. Verify both binaries are on PATH and respond to `--version`.

**Key interfaces (`orchestrator/adapters/base.py`):**

```python
from typing import Protocol, AsyncIterator
from .types import NexusRequest, NexusResponse, Token, HealthStatus

class Adapter(Protocol):
    name: str  # e.g. "anthropic"

    async def complete(self, request: NexusRequest) -> NexusResponse: ...
    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]: ...
    def count_tokens(self, text: str) -> int: ...
    def available_context(self) -> int: ...
    def estimated_cost(self, request: NexusRequest) -> dict: ...
        # returns {"pro_quota_pct": float, "usd": float}
    async def health_check(self) -> HealthStatus: ...
```

**Anthropic adapter behaviour (`orchestrator/adapters/anthropic.py`):**

- Default invocation: subprocess call to `npx ruflo claude-code --model claude-sonnet-4-6 --json --message "{prompt}"`. Stream stdout, parse JSON lines, yield tokens.
- If `use_ruflo: false` in config: subprocess call to `claude --model claude-sonnet-4-6 --json --message "{prompt}"` directly. Same parsing.
- Token counting: subscription mode does not return token counts; the adapter approximates from character count using a calibration ratio stored in `Config/tokenizer_ratios.yaml` (default Sonnet 4.6: 3.8 chars/token; calibrate against measured samples after Phase 1).
- Health check: spawn `claude --version` with 5-second timeout; if non-zero exit or timeout, return `unhealthy`.

**Event log behaviour (`orchestrator/vault/event_log.py`):**

- Each thread has its own JSONL file at `Logs/{thread-id-short8}.jsonl`.
- Each event is a JSON object with `event_id`, `timestamp`, `event_type` (`turn_user` | `turn_assistant` | `handover` | `metadata_update`), and a `payload` field.
- Writes are atomic appends with `O_APPEND` flag. No partial writes.
- The projection step reads the JSONL and rewrites the markdown live file (`Live/{date}_thread-{id}_{title}.md`) including frontmatter.

**Acceptance criteria for Phase 1:**

1. `pytest tests/` passes (event log, projection, adapter mocks).
2. Running the Obsidian plugin opens a chat view that accepts user input and streams a response from Sonnet 4.6.
3. The response is persisted to `Orchestrator/Live/{date}_thread-{id}_{title}.md` with valid YAML frontmatter conforming to the conversation schema (Deliverable 3).
4. The JSONL event log at `Logs/{id-short8}.jsonl` contains the user and assistant events.
5. Closing the plugin and reopening it restores the thread from the event log.
6. The adapter falls back from Ruflo to direct Claude Code invocation if `use_ruflo: false` is set in `Config/cascade.yaml`; both paths produce identical user-visible behaviour for Phase 1 scope.
7. No API key is required for Phase 1; the user is logged into Claude Pro via Claude Code only.

### Phase 2 — Cascade + handovers

**Outcome.** Failover works end-to-end across all five rungs. A manually-triggered failover (or a real `PRO_QUOTA_EXHAUSTED` event) produces a clean handover document and continues the conversation on the next rung.

**Additional repository structure:**

```
nexus/orchestrator/adapters/
├── google.py                      ← Gemini 3.1 Pro via AI Studio
├── openrouter.py                  ← GLM, Qwen, DeepSeek via OpenRouter
├── gmi.py                         ← optional gateway (disabled by default)
└── ollama.py                      ← local at localhost:11434

nexus/orchestrator/
├── cascade.py                     ← Cascade engine: rung selection, failover
├── triggers.py                    ← Trigger taxonomy + detectors
├── handover.py                    ← Handover document generation
└── summariser.py                  ← Wraps Haiku 4.5 via Anthropic adapter
```

**Additional dependencies:**
- `google-generativeai >= 0.7` (for Google AI Studio)
- Standard `httpx` for OpenRouter and Ollama (OpenAI-compatible APIs)

**Provider configuration template (`Config/cascade.yaml`):**

```yaml
schema_version: 2

rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
    use_ruflo: true
    ruflo_version: "{pinned-at-install}"
    models:
      default: claude-sonnet-4-6
      premium: claude-opus-4-7
      summariser: claude-haiku-4-5
    pro_quota_tracking: true
    api_fallback:
      enabled: false                # user opt-in via UI
      budget_cap_usd: 2.00

  - position: 2
    adapter: google
    model: gemini-3-1-pro
    auth_mode: api_key_free_tier
    thinking_level: medium

  - position: 3
    adapter: openrouter
    model: zhipu/glm-5.1:free
    fallback_models:
      - zhipu/glm-5:free
      - openai/gpt-oss-120b:free

  - position: 4
    adapter: openrouter
    model: qwen/qwen3-coder-480b:free
    fallback_models:
      - deepseek/deepseek-r1:free
      - meta/llama-4-scout:free
    use_for_intents: [coding, technical]

  - position: 5
    adapter: ollama
    base_url: http://localhost:11434
    model: "{detected-at-install}"
    keep_alive: -1

jurisdictions:
  rung_3_4_data_origin: cn-mixed-via-openrouter
  rung_5_data_origin: local
```

**Trigger taxonomy implementation notes:**

- `PRO_QUOTA_EXHAUSTED` is detected by Claude Code returning a quota-related error message. Claude Code surfaces these as specific stderr patterns; the adapter regex-matches. When detected, the orchestrator writes `pro_quota_exhausted_until: {next_monday_00z}` to the active thread's YAML and updates `Config/budgets.yaml` with the same timestamp.
- `OPENROUTER_QUOTA` is detected by OpenRouter HTTP 429 with body containing `"quota"` or `"daily limit"`. When detected, the orchestrator marks both R3 and R4 as exhausted until the OpenRouter reset (midnight UTC).
- `SOFT_DEGRADE` uses a rolling P95 latency baseline kept in memory per adapter; baseline updates on every successful call.
- All trigger events are logged to `Logs/orchestrator-{YYYY-MM-DD}.log` for debugging.

**Handover document generation (`handover.py`):**

The orchestrator's summariser (Haiku 4.5 via the Anthropic adapter, in subscription mode by default) is invoked with a templated prompt:

```
You are generating a handover document for a multi-model AI orchestration system.
The active model is being switched. Produce a Markdown document with exactly these
sections in order: ## Summary, ## Open Questions, ## Last User Intent,
## Recommended Continuation Style, ## Full History Path.

Constraints:
- Summary ≤ 500 words.
- Open Questions is a bulleted list of unresolved questions in the user's words.
- Last User Intent is one sentence describing what the user most recently asked for.
- Recommended Continuation Style is one paragraph guiding the next model on tone and posture.
- Full History Path is a wiki-link to the live conversation file.

Conversation transcript:
{full_transcript}

Style profile:
{style_profile_yaml}

Write the document now.
```

The output is written to `Handovers/{thread-id-short8}_handover_{seq}.md` with the YAML frontmatter from Deliverable 3.

**Acceptance criteria for Phase 2:**

1. All five adapters are implemented and pass per-adapter tests against mocked endpoints.
2. The cascade engine triggers failover on each of the seven trigger classes; tests cover each path.
3. A simulated `PRO_QUOTA_EXHAUSTED` event produces a handover document with all six required fields, writes it to `Handovers/`, and continues the conversation on Gemini 3.1 Pro.
4. The `use_ruflo: false` fallback path is verified — disabling Ruflo and confirming Rung 1 still operates via direct Claude Code invocation.
5. `cascade.yaml` is hot-reloadable: editing the file at runtime triggers the orchestrator to pick up changes without restart.
6. Pro quota tracking is functional and the running estimate is exposed via an orchestrator status endpoint.

### Phase 3 — Retrieval pipeline

**Outcome.** A new question retrieves relevant prior threads and vault notes; the active model receives them as packed context and cites them by wiki-link.

**Additional repository structure:**

```
nexus/orchestrator/retrieval/
├── __init__.py
├── indexer.py                     ← Builds SQLite + FAISS + BM25
├── embeddings.py                  ← Local Ollama embedding wrapper
├── searcher.py                    ← Hybrid BM25 + vector search
├── fusion.py                      ← Reciprocal Rank Fusion
├── reranker.py                    ← Haiku-based re-rank
└── packer.py                      ← Tier budget packing
```

**Additional dependencies:**
- `faiss-cpu >= 1.8` (or `faiss-gpu` if CUDA available)
- `rank-bm25 >= 0.2` (or Tantivy via PyO3 for performance)
- Embeddings via Ollama HTTP, not `sentence-transformers`

**Embedding model (Ollama):**
- Default: `nomic-embed-text` (768-dim).
- Alternative: `bge-m3` (1024-dim, multilingual).
- Onboarding (Phase 3 install step): `ollama pull nomic-embed-text`.

**Index build behaviour:**
- On Nexus startup, check if `Index/threads.sqlite`, `Index/embeddings.faiss`, and in-memory BM25 are present and current.
- If a markdown file under `Live/` or `Archive/` has been modified more recently than the index, schedule a background re-index.
- A full rebuild is invoked via `nexus rebuild-indices` and takes proportional time to vault size.

**Retrieval flow:**

```python
async def retrieve(query: str, k: int, filters: RetrievalFilters) -> list[RetrievalItem]:
    bm25_top = bm25_searcher.search(query, top=50)
    vec_top = await vector_searcher.search(query, top=50)
    fused = reciprocal_rank_fusion(bm25_top, vec_top)
    filtered = apply_filters(fused, filters)  # date, tag, jurisdiction
    reranked = await rerank_with_haiku(query, filtered[:20])
    return reranked[:k]
```

**Packer behaviour (`packer.py`):**

Implements the four-tier budget from Deliverable 2. Given the active adapter's `available_context()` and the retrieval results, the packer produces a single `NexusRequest` with system prompt, handover (if any), archived context, vault context, live history, and the current user turn.

**Acceptance criteria for Phase 3:**

1. Re-running an old question in a new thread surfaces the original thread in the retrieved context.
2. The active model includes wiki-links to retrieved sources in its response when relevant.
3. The packer never exceeds the active adapter's `available_context()` budget; verified via test.
4. Index integrity check on startup detects and reports drift between vault state and indices.
5. `nexus rebuild-indices` rebuilds all three indices from scratch.

### Phase 4 — Operational polish

**Outcome.** Portfolio-presentable UI/UX: per-turn Opus button, thread pin, Pro quota indicator, cost dashboard, cascade visualiser, conflict resolution UI, style normalisation layer, manual override controls.

**Additional plugin structure:**

```
plugin/views/
├── chat-view.ts                   ← already exists; extended
├── cascade-visualiser.ts          ← new collapsible panel
├── cost-dashboard.ts              ← new pane
├── thread-browser.ts              ← Dataview-powered table
└── conflict-resolver.ts           ← new modal for /Conflicts/

plugin/components/
├── model-pill.ts                  ← active-rung badge with R1 sub-indicator
├── quota-bar.ts                   ← Pro quota horizontal bar
├── opus-button.ts                 ← per-turn override
├── handover-divider.ts            ← in-line marker
└── toast.ts                       ← non-modal notifications
```

**Style-normalisation behaviour:**
- Thread's `style_profile` is read from YAML at every turn.
- Profile is prepended to the system prompt by every adapter (not by the orchestrator generically), because each adapter knows how to translate the profile into provider-specific system-prompt conventions.

**Conflict UI:**
- When the orchestrator detects an unresolvable concurrent write, the conflicting file goes to `Conflicts/{thread-id}_{timestamp}.md`.
- The plugin polls `Conflicts/` on a 5-second interval; on new entries, shows a non-modal banner offering the user a three-way merge view (current orchestrator state, conflict version, manual edit).

**Acceptance criteria for Phase 4:**

1. Per-turn Opus button routes the next turn to Opus 4.7, reverts to Sonnet after; thread YAML reflects the model in `models_used`.
2. Thread pin to a specific model persists across plugin restarts and disables cascade for the pinned thread until unpinned.
3. Cost dashboard shows Pro quota burn-down accurately within ±15% of measured ground truth.
4. Cascade visualiser reflects real-time rung status with status updates within 1 second of an underlying event.
5. A simulated concurrent write produces a `Conflicts/` entry and a UI banner.

### Phase 5 — Hardening and ship

**Outcome.** Install-and-forget V1, ready for portfolio presentation.

**Onboarding wizard (`onboarding/wizard.ts`):**

Steps, in order:
1. Welcome screen, brief overview.
2. Check Node.js >= 18; if absent, link to installer; halt until present.
3. Install Claude Code (`npm install -g @anthropic-ai/claude-code`); prompt user to `claude /login`. Verify auth by running `claude --version` and `claude --print "hi"` (one-token test).
4. Install Ruflo at pinned version; record version in `Config/ruflo.lock`.
5. Prompt user for Google AI Studio free-tier API key; verify by hitting Gemini 3.1 Pro with a test prompt.
6. Prompt user for OpenRouter free-tier API key; verify by listing free models.
7. Detect Ollama; if absent, offer to install. Detect VRAM via platform-specific tools (`nvidia-smi`, `system_profiler` on macOS, `glxinfo` on Linux). Recommend a Rung 5 model; pull on user confirmation.
8. Detect Obsidian Sync or git in the vault root; if either is active, write `.gitignore` entry and offer to configure Obsidian Sync selective-sync exclusion for `/Orchestrator/`.
9. Create `Config/` defaults; write `_DO_NOT_EDIT.md` sentinel.
10. Run a smoke test: ask "Test message" via the plugin, expect a Sonnet response, confirm it persists to vault.

**Error reporting:**
- All errors logged to `Logs/orchestrator-{YYYY-MM-DD}.log` and `Logs/plugin-{YYYY-MM-DD}.log`.
- No telemetry. No outbound error reporting.
- A `nexus diagnostics` CLI command bundles relevant logs + recent `Config/` state into a zip the user can attach to a GitHub issue.

**Documentation:**
- `README.md`: install instructions, architecture overview, screenshots.
- `docs/` folder: per-rung adapter docs, troubleshooting guide, cost/quota explanation, FAQ.
- A demo video script (not required as a build artifact, but documented).

**Acceptance criteria for Phase 5:**

1. A clean macOS, Linux, and Windows install completes successfully via the onboarding wizard without manual file editing.
2. `nexus diagnostics` produces a zip with the expected contents.
3. Documentation covers all five rungs, the trigger taxonomy, and the YAML schemas.
4. The plugin survives a 30-day soak test in single-user actual use without data loss, crashes, or unrecoverable state errors.

---

## Closing note

This document is sufficient for Claude Code to start executing Phase 1 tomorrow. Three things worth flagging:

- The cascade visualiser is the system's signature artifact. It makes the orchestration *legible*. Treat it as the hero element in Phase 4, not a sidebar widget.
- The handover UI moments — the toast, the in-line divider, the model-pill transition — are where the system either feels like magic or feels broken. Designing them takes more iteration than any other surface in the product.
- The cost / quota dashboard is the surface that earns user trust over time. It must look honest, not gamified. Treat it like a billing statement, not a fitness tracker.

The architecture is intentionally boring under the hood — append-only logs, hybrid retrieval, tiered packing, explicit trigger taxonomy, subscription-aware adapter, hot-reloadable cascade. Boring is good; the *experience* on top of boring is where the design work pays off.
