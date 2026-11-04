# Phase 2c — Health-Aware Pool Routing

## The problem this phase solves

The Phase 2a/2b cascade hardcodes three OpenRouter models at fixed rung positions (2, 3, 4). Free-tier model availability is volatile: on 2026-05-29 the live test showed `minimax`, `deepseek-v4-flash`, `qwen3-next`, and `llama-3.3-70b` all returning `404 / "Provider returned error"` while `gpt-oss-120b`, `nemotron-3-super`, and `gemma-4-31b` worked. Tomorrow the set could invert.

Two concrete failures observed:
1. **Dead models showed as "healthy."** The OpenRouter `health_check` does `GET /models` (API reachability) but never verifies a *specific* model responds. So a model whose provider has dropped it passes the health check, then 404s on the actual turn.
2. **A 404 was reported to the user as "rate-limited; brief cooldown."** The adapter's error classification conflates model-unavailable (404 / provider error) with rate-limit (429) with quota-exhausted (429 + quota keywords). The user sees a misleading message and the wrong cooldown gets applied.

A fixed cascade can't fix this. The answer is a **pool** of known-good free models plus **health-aware routing** that always picks the strongest model that is actually live right now, and correctly detects and times-out dead ones.

## What this phase delivers

- Correct error classification: model-unavailable vs rate-limit vs quota-exhausted, each with the right cooldown and the right user-facing message.
- A model **pool** in `cascade.yaml` (6-8 free models with tier + role tags), replacing the fixed rungs 2-4. Rung 1 (Sonnet, subscription) and the Ollama floor are unchanged.
- A **ModelHealth ledger** with exponential backoff: mark a model dead on real failure, retry automatically after backoff, clear on success. Quota-free (no constant probing).
- **Health-aware selection**: among live pool models, route by strength tier — so you always get the strongest available model with zero manual slug-swapping.
- **Cascade transparency** in the UI: see which model answered and which were skipped and why.
- A **task-classifier scaffold** (logging-only + opt-in role routing) that lays the groundwork for the eventual "best model for the question type" vision — without destabilizing this phase.

## What this phase deliberately does NOT do

- **Full task-based routing is not turned on by default.** T07 builds the classifier and logs its decisions; routing-by-task becomes the default in a later phase once there's real usage data to tune it. This keeps Phase 2c shippable.
- No proactive/continuous health probing. The free tier is ~200 requests/day shared across all OpenRouter free models; constant probing would burn the whole budget. Health is lazy (marked on real failure, retried after backoff). See T05 for the one quota-sane exception.
- No new providers (Groq/Cerebras/FreeLLMAPI). Logged for a future phase.
- No dashboard / project hub. That's a separate track.

## Cooldown / quota math (why lazy, not proactive)

OpenRouter free tier ≈ 20 req/min, ~200 req/day, shared across all `:free` models. Probing 8 models every 5 min = ~2,300 calls/day — impossible. Therefore:
- Mark a model dead **only on a real failure** during an actual turn (free — no extra calls).
- Exponential backoff: 5 min → 15 min → 60 min (capped), reset on success.
- When backoff expires the model is eligible again; the next real turn that selects it confirms or re-marks it. Zero extra quota cost.
- The only optional probe (T05) is a single 1-token check fired *only* at the moment a just-recovered top-tier model is about to be promoted, so the user doesn't eat a streaming failure on the recovery edge.

## The model pool (initial)

Strong + free as of 2026-05-29. Health-checking handles per-day availability; this is the candidate set, not a fixed cascade.

| Model | Tier | Roles | Context | Notes |
|---|---|---|---|---|
| `nvidia/nemotron-3-super-120b-a12b:free` | frontier | reasoning, coding | 1M | Leading AIME/SWE-bench among free; NVIDIA-hosted, reliable. Configurable thinking mode — set reasoning excluded or give token headroom. |
| `openai/gpt-oss-120b:free` | frontier | reasoning, coding, general | 131K | High-reasoning MoE, OpenAI-hosted, reliable. |
| `deepseek/deepseek-v4-flash:free` | frontier | reasoning, coding | 1M | Strong when its provider is up. |
| `qwen/qwen3-coder:free` | frontier | coding | 1M | Strongest free coding model when up. |
| `minimax/minimax-m2.5:free` | strong | coding, general | 200K | Frontier coding when its provider is up. Reasoning in separate field (adapter handles). |
| `qwen/qwen3-next-80b-a3b-instruct:free` | strong | general | 262K | Broad general fallback. |
| `google/gemma-4-31b-it:free` | reliable | general | 262K | Google-hosted, dependable mid-tier floor before Ollama. |
| `openai/gpt-oss-20b:free` | reliable | general, fast | 131K | Fast lightweight, OpenAI-hosted. |

Tier order for selection: `frontier` → `strong` → `reliable` → Ollama floor.

## Ticket order

```
P2c-T01  Error classification + real health_check        (root-cause fix; unblocks everything)
   ↓
P2c-T02  Model pool schema in cascade.yaml               (data model)
   ↓
P2c-T03  ModelHealth ledger (exponential backoff)        (state)
   ↓
P2c-T04  Health-aware selection engine                   (the routing brain)
   ↓
P2c-T05  Just-in-time recovery probe (optional, quota-sane)
   ↓
P2c-T06  UI: cascade transparency (who answered + why skipped)
   ↓
P2c-T07  Task-classifier scaffold (log-only + opt-in role routing)
   ↓
P2c-T08  Integration + acceptance
```

## Carry-forward bugs from Phase 2b

- **Opus heuristic wiring bug** — deferred and disabled. Out of scope here (Opus auto-escalation was dropped); leave disabled. Revisit only if Opus auto-routing is wanted later.
- **Web tools need paid ANTHROPIC_API_KEY** — separate investigation (Claude Code subprocess tool-use). Not in this phase.

## Phase 2c global acceptance criteria

1. `pytest tests/` passes (all existing + new).
2. A 404 / "Provider returned error" is classified as model-unavailable, marks that model dead, cascades to the next live model, and the user-facing message is accurate (not "rate-limited").
3. A real 429 with quota keywords applies the shared OpenRouter cooldown; a 429 without them applies a short per-model cooldown.
4. The pool loads from `cascade.yaml`; selection returns the strongest live model by tier.
5. A model marked dead is retried automatically after its backoff expires.
6. The UI shows which model answered and lists skipped models with reasons.
7. The task classifier logs a class for each turn; role routing works when the opt-in flag is on.
8. Live acceptance: with today's real availability, the cascade routes to a working frontier model without manual slug edits, and a forced model failure is detected and skipped correctly.
