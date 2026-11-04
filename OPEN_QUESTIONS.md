# Open Questions and Drift Log

This file accumulates discoveries that diverge from the PRD, deferred decisions, and known gaps. **Append-only.** Never delete or rewrite entries — append a new one referencing the old one if circumstances change.

Each entry format:

```
## [YYYY-MM-DD] [Ticket or context] — One-line summary

What was discovered:
...

What was substituted / decided:
...

Open for future review:
...
```

---

## [2026-05-15] Pre-Phase-1 verification — Ruflo CLI does not match PRD assumptions

**What was discovered:**

The PRD assumes Ruflo exposes a `claude-code` subcommand that wraps Claude Code with the user's Pro subscription (e.g., `npx ruflo claude-code --model X --print "prompt"`). This subcommand does not exist.

Ruflo at v3.7.0-alpha.38 is an entire multi-agent orchestration platform. Its CLI surface is built around primary commands: `init`, `start`, `agent`, `swarm`, `memory`, `task`, `session`, `mcp`, `hooks`, and advanced commands for neural training, hive-mind consensus, vector embeddings, etc. There is no thin "wrap Claude Code" mode.

Verified via `npx -y ruflo --help` on 2026-05-15. Output captured in `nexus-build/verification-2026-05-14.txt`.

**What was substituted / decided:**

- Phase 1 ships with `use_ruflo: false` as the default in `cascade.yaml`.
- The Anthropic adapter invokes Claude Code directly: `claude --model claude-sonnet-4-6 --print --output-format stream-json "{prompt}"`. The user is already authenticated via Claude Pro through Claude Code's own `/login` flow.
- Ruflo remains a documented optional integration. If someone wants to use Ruflo's MCP server, agent spawning, or memory features alongside the Nexus in a future phase, the path is `ruflo init` in a project directory and configuring the Nexus to talk to Ruflo's MCP server — not subprocess wrapping. This is a Phase 5+ concern, not Phase 1.
- The `use_ruflo: true` config path in the Anthropic adapter (P1-T05) is documented but not exercised in Phase 1. It exists as the seam for a future integration that would need to be designed against Ruflo's actual API.

**Open for future review:**

- If Ruflo's value (cache-aware token tracking, prompt compression, swarm-based context management) becomes load-bearing for cost control, revisit the integration approach. The path would be MCP-based, not CLI-wrapped.
- Phase 2 onwards may want to revisit whether *any* of Ruflo's features (memory layer, embeddings, agent spawning) overlap usefully with the Nexus's own retrieval/cascade work. Right now they appear parallel, not synergistic.

---

## [2026-05-15] Pre-Phase-1 verification — Claude Code CLI flags captured

**What was discovered:**

The PRD's adapter sketch used `--message "prompt"` as the prompt-passing flag and `--json` as the output format. Both are wrong. Real flags from `claude --help` (Claude Code v2.1.71):

- The prompt is a positional argument, not a flag value.
- Non-interactive / print mode: `-p` or `--print`.
- Streaming output format: `--output-format stream-json` (other options: `text`, `json`).
- Model selection: `--model claude-sonnet-4-6` (alias `sonnet` accepted; full model strings also work).
- Authentication: `claude /login` (subcommand, not a CLI flag — confirmed via the `auth` command in help).

**What was substituted / decided:**

P1-T05's adapter `_build_command` uses:

```
claude --model {model} --print --output-format stream-json "{prompt}"
```

with long prompts piped on stdin where argv length is a concern (Claude Code accepts stdin input via `--input-format stream-json` or simply standard piping when `--print` is set).

**Open for future review:**

- Claude Code 2.x is still under active development. Re-verify flags at each phase boundary.
- The `--max-budget-usd` flag (only works with `--print`) may be useful for the cost-runaway guard rail in Phase 4.

---

## [2026-05-15] P1-T05 — `--output-format stream-json` requires `--verbose`

**What was discovered:**

Running `claude --print --output-format stream-json` without `--verbose` exits with:
`Error: When using --print, --output-format=stream-json requires --verbose`

The `--verbose` requirement is not documented in `claude --help`. It was discovered by running the
actual command (Claude Code v2.1.142).

**What was substituted / decided:**

`_build_command` always includes `--verbose` when `--output-format stream-json` is set.

**Open for future review:**

Re-check at each phase boundary — the `--verbose` requirement may be lifted in a future release.

---

## [2026-05-15] P1-T05 — `stream-json` event format captured from real run

**What was discovered:**

`claude --print --output-format stream-json --verbose --no-session-persistence` emits one JSON
object per line. Actual event types observed (Claude Code v2.1.142, Sonnet 4.6):

- `{"type":"system","subtype":"init",...}` — init metadata; contains model, session_id, tools, apiKeySource. Not a token; skip.
- `{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"...","signature":"..."}],...},...}` — extended thinking block; skip (not user-visible text).
- `{"type":"assistant","message":{"content":[{"type":"text","text":"<actual reply>"}],...},...}` — text token; yield `Token(text=..., is_final=False)`.
- `{"type":"rate_limit_event","rate_limit_info":{...},...}` — quota info; skip.
- `{"type":"result","subtype":"success","is_error":false,"result":"<full reply>","stop_reason":"end_turn",...}` — stream end; yield `Token(text="", is_final=True)`.

For error results: `{"type":"result","subtype":"...","is_error":true,...}` → raise `AdapterUnavailable`.

The `stop_reason` on intermediate `assistant` events is `null`; only the final `result` event carries `"end_turn"`.

`apiKeySource: "none"` in the init event confirms subscription (Claude Pro) mode, not direct API key.

**What was substituted / decided:**

`_parse_stream_line` pattern:
1. `type == "assistant"` → extract `content` blocks where `block["type"] == "text"` → yield Token.
2. `type == "result"` and `subtype == "success"` → yield `Token(text="", is_final=True)`.
3. `type == "result"` and `is_error` or `subtype != "success"` → raise `AdapterUnavailable`.
4. Everything else → `None` (skip silently, log at DEBUG).

**Open for future review:**

- Claude Code may add delta-level streaming (one token per line) in a future release, which would change the parser. Current output sends one `assistant` event per complete response turn.
- The `thinking` content block may be suppressible via a flag; investigate before Phase 2.
- `total_cost_usd` appears in the `result` event even in subscription mode. This could feed Phase 4 quota tracking if the value is meaningful for Pro users.

---

## [2026-05-15] P1-T06 — UUID7 log-path collision when two threads created in the same millisecond

**What was discovered:**

`data-architecture.md` shows the log filename as `a1b2c3d4.jsonl` (8 hex chars). UUID7's first 8 hex chars cover only the millisecond-level timestamp. Two threads created in the same millisecond share the same 8-char prefix and therefore the same JSONL path, silently merging their event logs.

Discovered during the `test_returns_multiple_entries` test, which generates two UUIDs in rapid succession.

**What was substituted / decided:**

`_log_path` now uses the first 16 hex chars of the thread_id (with dashes removed). The 16th hex character falls within the UUID7 sequence/version field, making the filenames unique across same-millisecond calls. The `projection.py` `thread_filename` still uses 8 chars (only affects the Live/ markdown filenames, which are derived projections, not canonical sources).

**Open for future review:**

- The 8-char ID used in `projection.py`'s `thread_filename` could also collide for same-ms threads, producing an overwritten Live/ markdown. This is acceptable in Phase 1 (single-user, rare scenario). Consider bumping projection filenames to 16 chars in Phase 2.
- Evaluate whether to expose the short ID (8 or 16 chars) or the full UUID in the public API surface (threads list, plugin display) for better usability vs. uniqueness.

---

## [2026-05-15] Phase 1 live test — CORS preflight bug

**What was discovered:**

The Phase 1 acceptance tests passed because they used Python's `httpx`, which doesn't send CORS preflight requests. Obsidian's plugin runtime treats `fetch()` to `127.0.0.1:7777` as a cross-origin call from `app://obsidian.md`, sends an `OPTIONS` preflight, and the server's lack of CORS handler returns 405. The plugin reports "failed to fetch" with no useful error.

**What was substituted / decided:**

Patched `orchestrator/server.py` to include `CORSMiddleware` allowing `app://obsidian.md` and localhost origins. Restart of orchestrator required.

**Open for future review:**

- Phase 2 should add an integration test that uses a real browser-like fetch (or just a manual `curl -X OPTIONS` precheck) to catch CORS regressions.
- The acceptance criteria for Phase 1 mentions "the plugin renders" and "end-to-end streamed turn" but the integration tests used `httpx` for the latter — that gap let this slip through. Future phases should test from the actual plugin runtime when possible.

## [2026-05-15] Feature request — web tools and research-style reasoning

**What was discussed:**

User wants the Nexus to "think like a research assistant" — search the web for current information, verify before claiming facts, synthesize from multiple sources. This is tool-use, separate from the retrieval pipeline.

**Open for future review:**

- Add web_search and web_fetch tools to the Anthropic adapter in Phase 3 or as Phase 2.5.
- Sonnet 4.6 natively supports tool use via its API. The adapter just needs to declare tools and route results.
- Distinct from vault retrieval (which is the "remember what we discussed" path). Both should land in Phase 3.

## [2026-05-15] Feature request — "Continue from vault note" command

**What was discussed:**

While writing in a regular Obsidian note, the user often wants to ask Sonnet about it without breaking flow. Currently the only path is: open Nexus Chat → manually copy-paste the note content → ask. The chat has no awareness of which note triggered it.

**Proposed shape:**

- Right-click context menu on any markdown note: "Open in Nexus Chat"
- Command palette entry: "Nexus: Chat about current note"
- Behaviour: opens the chat view with the note's content pre-loaded as the first user turn's context (in the system prompt or as a reference, not literally typed in), and creates a thread linked back to the source note via `related_notes` in YAML.

**Open for future review:**

- Phase 4 UX work. Touches the plugin (new command + context menu) and the orchestrator's prompt construction (note content needs to flow into `build_system_prompt` or similar).
- Big daily-use UX unlock — this is probably the single most-used feature once it ships.
- Decide: does the chat thread auto-archive when the source note hasn't been touched in N days? Or does the link stay live forever?

---

## [2026-05-15] Feature request — Auto-archive of stale threads

**What was discussed:**

`Orchestrator/Live/` accumulates every conversation forever. The PRD specs an `Archive/{YYYY}/{MM}/` folder structure but no rule for when threads move there. Without auto-archive, after months of use `Live/` will have hundreds of files; retrieval indices grow unnecessarily and the human-readable thread browser becomes unusable.

**Proposed shape:**

- Background job (runs at orchestrator startup, then daily): scan `Live/`, move any thread whose `updated` is older than N days (default 30) to `Archive/{YYYY}/{MM}/`.
- Update the thread's YAML: `status: live` → `status: archived`.
- Update any inbound wiki-links in `related_threads` of other threads.
- Archived threads remain searchable via retrieval, just live in a different folder.

**Open for future review:**

- Phase 3 or 4. Retrieval pipeline (Phase 3) needs to know archived threads exist; auto-archive logic itself is Phase 4 hygiene.
- N=30 days is a guess. Worth seeing actual usage patterns first.
- Manual override: user should be able to "pin to Live" any thread they don't want auto-archived.
- The wiki-link update is non-trivial — needs to rewrite related threads' YAML when targets move. Punt: leave the wiki-links pointing at the old path; Obsidian's link-update prompts will handle the rest. (Confirm this behaves sanely.)

---

## [2026-05-15] Design question — Opus auto-escalation heuristic

**What was discussed:**

The PRD ships Phase 1 with Opus 4.7 as user-pin-only — never auto-invoked. The user has expressed wanting "best-researched answer every time," which implies wanting Opus more often. Tension: Pro quota burns 3-5× faster on Opus, so "always Opus" exhausts quota in days.

**Possible heuristics:**

- *Long context*: if prompt + retrieved context exceeds N tokens, escalate to Opus.
- *Multi-step reasoning markers*: if the user's message contains "design", "architect", "compare", "trade-off", or asks for a structured deliverable, escalate.
- *Tool-use intent*: if the model will need web search or multi-step research, escalate.
- *Output budget*: if the expected output is >4K tokens (long writing, code generation), escalate.
- *User-tagged*: a `#opus` tag in the message forces Opus for that turn.

**Open for future review:**

- Phase 2 design boundary. The cascade engine will need to know about per-turn model selection within Rung 1 (Sonnet vs Opus), not just rung-to-rung failover.
- Worth deciding before Phase 2 starts. Recommendation: ship Phase 2 with `#opus` tag + per-turn button only (no heuristic), then observe which turns the user *actually* escalates, then build a heuristic from real signal. Don't speculate.
- The Pro quota indicator (Phase 4) feeds into this — auto-escalation should soften when quota is below 30%.

---

## [2026-05-15] Feature request — Background research mode

**What was discussed:**

A "research this overnight" mode where the user queues a question and the orchestrator works on it asynchronously — searches the web (assuming web-tools land in Phase 3), reads multiple sources, drafts an answer, leaves it in the vault as a fully-cited note for the user to read in the morning.

**Proposed shape:**

- Command: "Nexus: Queue research task"
- Input: a question or topic prompt, optional time budget, optional max-cost.
- Orchestrator runs the task in the background (via a job queue — could be as simple as a SQLite-backed queue + a worker thread).
- Result lands as a new note in `Orchestrator/Research/{date}_{slug}.md` with full source citations and a YAML field indicating it was background-generated.
- User gets a notification (Obsidian's `new Notice()` or a system notification via `osascript`).

**Open for future review:**

- Phase 5+ territory. Depends on: web tools (Phase 3), Opus auto-escalation (Phase 2-4), cost ledger (Phase 4).
- Real-cost question: a single overnight research task could burn substantial Pro quota or, if escalated to API, real dollars. Hard cost cap per task is non-negotiable.
- This is the most "agentic" feature in the roadmap. Worth being honest about scope creep — it's a separate product surface, not a Nexus feature, arguably. Could justify its own phase.
- If shipped, it's also a strong portfolio artifact: "Obsidian Nexus also does autonomous research." Worth keeping in the roadmap even if it lands late.

---

## [2026-05-16] P2a-T02 — google-genai SDK package and model string diverge from PRD

**What was discovered:**

1. **SDK package**: The PRD references `google-generativeai>=0.7`. That package (the legacy SDK) is superseded by `google-genai` (the next-generation SDK). At build time the current version is `google-genai==2.3.0`. The two packages have incompatible APIs: `google-genai` uses `genai.Client(api_key=...)` + `client.aio.models.*` for async, vs the old SDK's `genai.configure()` + module-level calls. Verified via `pip show google-generativeai` (not found) and `pip show google-genai` (v2.3.0 installed) on 2026-05-16.

2. **Model string**: The PRD writes `gemini-3-1-pro`. The actual API model string is `gemini-3.1-pro-preview` (note the dots, not dashes, and the `-preview` suffix). Verified by listing models via `client.models.list()` — `models/gemini-3.1-pro-preview` appears with `display_name: Gemini 3.1 Pro Preview`, `input_token_limit: 1,048,576`, `output_token_limit: 65,536`.

3. **Free-tier quota is zero for Gemini 3.1 Pro and 2.5 Pro**: Both `gemini-3.1-pro-preview` and `gemini-2.5-pro` return HTTP 429 RESOURCE_EXHAUSTED with `limit: 0` for this AI Studio project. The quota IDs in the error are `GenerateRequestsPerDayPerProjectPerModel-FreeTier` and `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`. `gemini-flash-latest` (which resolves to `gemini-3-flash-preview` at runtime) IS accessible on the free tier. The PRD's claim of "~15 RPM, ~1500 RPD on Gemini 3.1 Pro free tier" does not reflect this account's quota.

4. **Streaming format**: The SDK abstracts SSE entirely. `await client.aio.models.generate_content_stream(model, contents, config)` returns an async iterable of `GenerateContentResponse` objects. Token text is at `chunk.candidates[0].content.parts[i].text`. Intermediate chunks: `finish_reason=None`. Final chunk: `finish_reason=FinishReason.STOP`, `text=""` (may include a binary `thought_signature`). Safety blocks: `finish_reason=FinishReason.SAFETY` in the response body (not raised as an exception). Error classes: `google.genai.errors.ClientError` (4xx), `google.genai.errors.ServerError` (5xx). Captured to `tests/fixtures/google_stream.json`.

5. **Async count_tokens**: `await client.aio.models.count_tokens(model, contents)` returns an object with `.total_tokens`. This endpoint is free and does not count against generation quota. Verified to work with `gemini-flash-latest`.

**What was substituted / decided:**

- The adapter uses `google-genai>=2.3` (added to `pyproject.toml`).
- Default model string set to `gemini-3.1-pro-preview` (the PRD's intended model, correct API string). The adapter will immediately hit quota=0 on this account and raise `AdapterQuotaExhausted`, which the cascade will treat as a `PRO_QUOTA_EXHAUSTED`-class trigger (actually `OPENROUTER_QUOTA` equivalent — see trigger taxonomy). The architecture is correct; only the free-tier access is missing.
- `gemini-flash-latest` used for smoke test and format verification since it is accessible on free tier.

**Open for future review:**

- If this project's AI Studio free-tier quota for pro models is lifted (or if a paid tier is enabled), change nothing in the adapter — the model string and SDK calls are already correct.
- Alternatively, set `gemini-flash-latest` as the Rung 2 default in `Config/cascade.yaml` for development; upgrade to `gemini-3.1-pro-preview` once quota is available.
- Re-verify per-minute and per-day rate limits at each phase boundary — they are account-specific and the API does not expose them programmatically.
- The cascade's `OPENROUTER_QUOTA` trigger class may need a parallel `GOOGLE_QUOTA` class for the Rung 2 case. Currently `AdapterQuotaExhausted` maps to `PRO_QUOTA_EXHAUSTED` in the trigger taxonomy (P2a-T06 to decide).


## [2026-05-15] Cascade revision — Drop Gemini, restructure around OpenRouter free-tier frontier models

**What was discovered:**

After P2a-T02 surfaced that `gemini-3.1-pro-preview` has `limit: 0` on the user's free tier (effectively dead at Rung 2), the wider model landscape was reviewed. Per OpenRouter rankings, May 2026 benchmarks, and direct verification:

- **MiniMax M2.5** (OpenRouter `minimax/minimax-m2.5:free`) scores 80.2% on SWE-bench Verified — matches Claude Opus 4.6 (80.8%). Confirmed free and reachable on the user's OpenRouter key via live curl.
- **GLM-5** (verify slug — possibly `z-ai/glm-5:free` or similar) hits Tier B (77.8% SWE-bench). NOTE: GLM-5.1 regressed to Tier C and should NOT be used despite the version number being higher.
- **Qwen 3.6 Plus Preview** is currently free during preview; strong hybrid arch, 78.8% SWE-bench.
- Gemini 3.1 Pro Preview is not callable on this account; Gemini Flash works but is meaningfully weaker than the OpenRouter free options.

**What was substituted / decided:**

The active cascade for Phase 2a integration tests (T11) and the shipping `cascade.yaml` default will be:

| Rung | Model | Provider |
|---|---|---|
| 1 | claude-sonnet-4-6 (default), claude-opus-4-7 (on-demand), claude-haiku-4-5 (summariser) | Anthropic via Pro |
| 2 | minimax/minimax-m2.5:free | OpenRouter |
| 3 | GLM-5 :free slug (verify in T03) | OpenRouter |
| 4 | Qwen 3.6 Plus :free slug (verify in T03) | OpenRouter |
| 5 | gemma4:latest or qwen2.5-coder:7b | Local Ollama |

Gemini drops out of the active cascade entirely.

The Google adapter built in P2a-T02 stays in the codebase. It's a working, tested adapter — kept as future work for when Google's free-tier story changes, or for users who configure their own `cascade.yaml` differently. It is not wired into the shipping `cascade.yaml`.

**Implications:**

- Three of the four cloud rungs share one OpenRouter free key. The OpenRouter daily limit (~200 req/day on free tier) now covers Rungs 2, 3, and 4 combined. When OpenRouter quota fires, the cascade falls from Sonnet straight to local Ollama. The `OPENROUTER_QUOTA` trigger class in P2a-T06 handles this correctly via `cross_rung_cooldown=True` — verify that the cooldown key (`openrouter_shared`) covers all three rungs, not just two.
- MiniMax M2.5 returns a `reasoning` field in addition to `content` — the OpenRouter adapter (P2a-T03) must consume only `content`, not concatenate reasoning into the visible response. Test fixture should include a response with both fields.
- `cascade.yaml` schema currently has one `model` field per rung. For Rung 1 there's a separate `models: {default, premium, summariser}` block. The other rungs only need one slug each. No schema change required for the revised cascade.

**Open for future review:**

- If MiniMax M2.5 gets removed from the OpenRouter free tier, fall back to one of: Kimi K2.5 (:free), DeepSeek V4 Flash (very cheap, near-free), or GLM-5. The cascade engine doesn't auto-discover fallbacks; the user would edit `cascade.yaml`.
- Qwen 3.6 Plus is free *during preview*. When Alibaba turns on pricing, this rung will start failing with quota errors. At that point, sub a different free OpenRouter model.
- Provider diversity is reduced. Sonnet → 3× OpenRouter → local means an OpenRouter outage drops three rungs. Phase 3+ might reconsider adding a Google or DeepSeek-direct adapter for provider diversity if OpenRouter reliability becomes an issue.
- The OpenRouter free-tier model landscape is volatile (per the May 2026 articles). T11's integration tests should include a "verify current slugs are still live" step rather than hardcoding indefinitely.

---

## [2026-05-16] P2a-T03 — OpenRouter adapter verification: slug substitutions, streaming format, reasoning token handling

**What was discovered:**

**Model slugs (hard gate, live-verified 2026-05-16):**

1. `zhipu/glm-5.1:free` — NOT available on OpenRouter free tier. The model listing returns 400 "not a valid model ID" for this slug.
   - Substitute → `z-ai/glm-4.5-air:free` (GLM 4.5 Air, context=131,072 tokens). Confirmed reachable, free, cost=0.
   - Note: GLM-5.1 shows as Tier C in OpenRouter rankings; GLM 4.5 Air is the current strong offering from Zhipu.

2. `qwen/qwen3-coder-480b:free` — NOT available in free tier at verification time (May 2026).
   - Substitute → `qwen/qwen3-coder:free` (Qwen3 Coder 480B A35B, context=1,048,576 tokens). Confirmed reachable, free.
   - Warning: Qwen3 Coder is free during preview. When Alibaba enables pricing, this rung will fail with quota errors. Monitor at each phase boundary.

3. `minimax/minimax-m2.5:free` — Confirmed available and reachable. At runtime resolves to `minimax/minimax-m2.5-20260211:free`. Cost=0. Free key limit: `limit: null`. SWE-bench Verified: 80.2%.

**Rate limits:** The OpenRouter free tier shares ~200 RPD and ~20 RPM across all three rungs combined. Both MiniMax M2.5 and Qwen3-Coder returned upstream 429s during multi-request testing — confirming the shared pool constraint. The `cooldown_key="openrouter_shared"` design is correct.

**Streaming SSE format (live-verified):**
- Each SSE chunk: `data: {json}\n\n`
- Keepalive: `: OPENROUTER PROCESSING` — must be skipped
- Terminator: `data: [DONE]`
- Content chunk: `choices[0].delta.content = "text"` (non-empty)
- Reasoning chunk: `choices[0].delta.content = ""`, `choices[0].delta.reasoning = "text"` — MUST be skipped (reasoning tokens not visible to user)
- Stop chunk: `choices[0].finish_reason = "stop"`, delta empty
- Both MiniMax M2.5 and GLM 4.5 Air are reasoning models and emit reasoning chunks before content chunks. GLM 4.5 Air with `max_tokens=10` returned `content: null` because the model was cut off during reasoning; use `max_tokens>=500` to get populated content.

**Model-not-found format:**
- HTTP 400 (not 404) with body: `{"error": {"message": "<slug> is not a valid model ID", "code": 400}}`
- This is the trigger for `_ModelNotAvailable` and fallback retry in the adapter.

**Fixture captured:** `tests/fixtures/openrouter_stream.txt` — represents a MiniMax M2.5 response with one keepalive, one reasoning chunk, one content chunk, and a stop chunk.

**What was substituted / decided:**

- Rung 2: `minimax/minimax-m2.5:free` (unchanged from cascade revision entry)
- Rung 3: `z-ai/glm-4.5-air:free` (was `zhipu/glm-5.1:free` in PRD)
- Rung 4: `qwen/qwen3-coder:free` (was `qwen/qwen3-coder-480b:free` in PRD)
- All three use `OpenRouterAdapter` with the same key and `cooldown_key="openrouter_shared"`
- `fallback_models` parameter wired into the adapter constructor; cascade config can specify per-rung fallbacks

**Open for future review:**

- Verify all three slugs are still available at each phase boundary (T11 acceptance step).
- If Qwen3 Coder pricing turns on, likely fallback: `deepseek/deepseek-r1:free` or `meta-llama/llama-3.3-70b-instruct:free`.
- GLM 4.5 Air context window (131,072) is much smaller than Qwen3 Coder (1M). Rung 3 will hit `AdapterContextOverflow` earlier than Rung 4 for long threads.
- MiniMax M2.5 upstream rate limits (HTTP 429 from the upstream provider, not OpenRouter) were observed during testing. The adapter correctly maps these to `AdapterRateLimit` (transient, retry-after) vs `AdapterQuotaExhausted` (daily pool).

---

## [2026-05-16] P2a-T04 — Ollama adapter verification: streaming format, keep_alive integer vs string

**What was discovered:**

1. **Ollama is running and both models are confirmed pulled** (verified via `GET /api/tags`):
   - `gemma4:latest` — 8.0B Q4_K_M GGUF, 9.6 GB, family: gemma4
   - `qwen2.5-coder:7b` — 7.6B Q4_K_M GGUF, 4.7 GB, family: qwen2
   - Ollama version at time of test: inferred from behavior (LaunchAgent already configured from Phase 1)

2. **Streaming format: NDJSON (not SSE).**
   Each line is a self-contained JSON object. There is no `data:` prefix, no keepalive lines, no `[DONE]` terminator. Final line has `"done":true` and `"done_reason":"stop"` with `"content":""`.
   Format captured to `tests/fixtures/ollama_stream.txt`:
   ```
   {"model":"gemma4:latest","created_at":"...","message":{"role":"assistant","content":"Hello"},"done":false}
   {"model":"gemma4:latest","created_at":"...","message":{"role":"assistant","content":","},"done":false}
   {"model":"gemma4:latest","created_at":"...","message":{"role":"assistant","content":""},"done":true,"done_reason":"stop","total_duration":...,"eval_count":7}
   ```

3. **`keep_alive: "-1"` (string) is rejected by Ollama with "time: missing unit in duration '-1'".**
   Ollama's Go backend calls `time.ParseDuration` on string values, which requires a unit suffix (e.g. "5m", "1h"). The integer `-1` is treated specially (maps to `math.MaxInt64`, i.e. never unload). The fix: `keep_alive: int | str = -1` (integer default). Duration strings like `"5m"` still work for callers who want explicit timeouts.

4. **Cold-start latency:** Loading `gemma4:latest` from disk takes ~25 seconds (`load_duration: 25228112791` ns). With `keep_alive=-1`, subsequent requests respond in ~2.5 seconds. The warm-start benefit is real and significant for Rung 5.

5. **Smoke test output (warm model, 2026-05-16):**
   ```
   content: 'Hello there, how are you doing?'
   model_id: ollama:gemma4:latest
   auth_mode: local
   finish_reason: stop
   duration_ms: 2482
   ```

**What was substituted / decided:**

- Ticket spec used `keep_alive: str = "-1"`. Changed to `keep_alive: int | str = -1` (integer default) to match Ollama's actual API contract.
- `context_window` default stays at 32,000 (conservative; gemma4 likely supports ~128K but Ollama's num_ctx default is 2048 unless overridden in the Modelfile).

**Open for future review:**

- The effective context window for `gemma4:latest` depends on the Modelfile's `PARAMETER num_ctx`. The default is 2048 in many Ollama installs. To use a larger context, either edit the Modelfile or pass `"options": {"num_ctx": 32768}` in the payload. Phase 2a's `OllamaAdapter` does not yet expose `num_ctx` as a parameter — this is a gap. Consider adding it in Phase 3 when longer retrieved contexts are injected.
- Health check uses `GET /api/tags` (no generation cost). If Ollama adds an authenticated endpoint in future versions, the health check will need updating.
- `qwen2.5-coder:7b` is the low-memory fallback. At ~4.7 GB it can coexist with other processes more easily. Cascade config can select it via `model: qwen2.5-coder:7b`.


## [2026-05-15] Cascade revision — Correction after live OpenRouter API query

**What was discovered:**

The earlier cascade revision (same date, entry above) named "GLM-5" and "Qwen 3.6 Plus" as Rung 3 and Rung 4. Direct query of `https://openrouter.ai/api/v1/models` filtered to free-tier shows those slugs are NOT on the free tier. The full set of free-tier non-Anthropic frontier models on OpenRouter as of 2026-05-15:

```
deepseek/deepseek-v4-flash:free                DeepSeek V4 Flash
minimax/minimax-m2.5:free                      MiniMax M2.5
qwen/qwen3-coder:free                          Qwen3 Coder 480B A35B
qwen/qwen3-next-80b-a3b-instruct:free          Qwen3 Next 80B A3B Instruct
z-ai/glm-4.5-air:free                          Z.ai GLM 4.5 Air
```

**What was substituted / decided (supersedes the previous cascade entry):**

Phase 2a shipping `cascade.yaml` defaults:

| Rung | Model | Provider | Notes |
|---|---|---|---|
| 1 | claude-sonnet-4-6 / claude-opus-4-7 / claude-haiku-4-5 | Anthropic via Pro | Unchanged |
| 2 | minimax/minimax-m2.5:free | OpenRouter | 80.2% SWE-bench, matches Opus 4.6 |
| 3 | deepseek/deepseek-v4-flash:free | OpenRouter | 78% SWE-bench, 1M context, hybrid attention |
| 4 | qwen/qwen3-next-80b-a3b-instruct:free | OpenRouter | 80B general-purpose instruct (broader than qwen3-coder) |
| 5 | gemma4:latest or qwen2.5-coder:7b | Local Ollama | Sovereignty floor |

P2a-T03's OpenRouter adapter already exists and is rung-agnostic. No code change — the slugs live in `cascade.yaml`. The `fallback_models` parameter on each instance should include the other two OpenRouter free slugs as fallbacks, so if any slug gets removed or renamed mid-cascade, the adapter has alternatives.

**Why these picks over alternatives:**

- DeepSeek V4 Flash > GLM-4.5-Air at Rung 3: DeepSeek V4 Flash is purpose-built for high-throughput agentic workloads, scores 78% on SWE-bench, supports 1M context, has hybrid attention. GLM-4.5-Air is the explicitly-lightweight variant of the GLM family and is meaningfully weaker.
- Qwen3-Next-80B > Qwen3-Coder at Rung 4: Coder models are noticeably weaker on non-coding tasks. The Nexus is a general-purpose assistant, not a coding-only assistant. Qwen3-Next is the broader, more useful general fallback.

**Open for future review:**

- If MiniMax M2.5, DeepSeek V4 Flash, or Qwen3-Next get removed from the free tier (free model availability is volatile per the OpenRouter team's own caveat), substitute from the remaining free list. Kimi K2.5 and K2.6 keep appearing in benchmarks but are NOT on the free tier as of this date.
- Re-verify the free-tier slug list at every phase boundary. Phase 2b should re-query the OpenRouter API before final config commits.
- The OpenRouter shared-quota concern stands: three rungs on one ~200 RPD free pool. When that pool drains, the cascade falls straight to local Ollama. Acceptable for Phase 2a; Phase 4 may want a paid OpenRouter tier or a separate API key per rung for capacity scaling.


## [2026-05-15] Cascade revision — FINAL after live behavioral testing (supersedes prior 2026-05-15 entries)

**Method:**

Tested three candidate Rung 2-4 models against the OpenRouter free tier with real prompts. Measured (a) response latency, (b) whether `reasoning` is separated from `content` in the response payload, (c) clean finish_reason, (d) actual output quality.

**Results:**

| Slug | Latency | Reasoning separation | Finish | Verdict |
|---|---|---|---|---|
| `minimax/minimax-m2.5:free` | 9s | Clean — `reasoning` in separate field | stop | ✓ Use |
| `openai/gpt-oss-120b:free` | 19s | Clean — minimal reasoning tokens (9), all in content cleanly | stop | ✓ Use |
| `nvidia/nemotron-3-super-120b-a12b:free` | 5s | **BROKEN** — reasoning bleeds into `content` field as visible monologue | length (hit cap) | ✗ Skip |
| `deepseek/deepseek-v4-flash:free` | not benchmarked | unknown, but provider diverse | — | Reserve as Rung 4 |

Nemotron's response showed the model writing its internal thought process (`"Okay, the user wants..."`, `"*checks draft* At 98 words now..."`) into `content`, never reaching a final answer at 200-token cap. This requires Nemotron-specific stripping logic that the OpenRouter adapter doesn't have and isn't worth building.

**FINAL cascade for Phase 2a (locked):**

| Rung | Slug | Context | Provider |
|---|---|---|---|
| 1 | claude-sonnet-4-6 (default) / claude-opus-4-7 (on-demand) / claude-haiku-4-5 (summariser) | 200K / 1M / 200K | Anthropic via Pro |
| 2 | `minimax/minimax-m2.5:free` | 200K | OpenRouter |
| 3 | `openai/gpt-oss-120b:free` | 131K | OpenRouter |
| 4 | `deepseek/deepseek-v4-flash:free` | 1M | OpenRouter |
| 5 | `gemma4:latest` | local | Ollama |

The `fallback_models` parameter on each OpenRouter adapter instance should include the other two slugs as fallbacks. If MiniMax fails, try gpt-oss; if gpt-oss fails, try DeepSeek V4 Flash.

**Models that were considered and dropped, with reasons:**

- `gemini-3.1-pro-preview` — limit:0 on free tier (Rung 2 originally)
- `gemini-flash-latest` — meaningfully weaker than OpenRouter free options on benchmarks
- `z-ai/glm-4.5-air:free` — lightweight variant, weaker than alternatives
- `z-ai/glm-5*` — not on OpenRouter free tier
- `qwen/qwen3-coder:free` — coding-specialist, weaker on general tasks; Qwen3-Next-80B-Instruct is broader and a possible future swap
- `qwen/qwen3-next-80b-a3b-instruct:free` — strong, untested, reserved as fallback option
- `nvidia/nemotron-3-super-120b-a12b:free` — reasoning-bleeds-into-content
- `meta-llama/llama-3.3-70b-instruct:free` — Nemotron and MiniMax dominate it on benchmarks
- `nousresearch/hermes-3-llama-3.1-405b:free` — unaligned creative finetune, wrong fit for a research assistant
- All Gemma 4 / Liquid / Baidu / Poolside entries — smaller or specialized, not Sonnet-substitutes

**Adapters built but not in active cascade:**

- `orchestrator/adapters/google.py` (P2a-T02) — tested, working, kept for future configs.

**Open for future review:**

- All three OpenRouter rungs share one ~200 RPD pool. Phase 4 should consider a paid tier or per-rung API keys.
- Re-test all slugs at every phase boundary; free-tier availability shifts.
- DeepSeek V4 Flash Rung 4 untested in behavioral curl — verify before P2a-T11 acceptance.
- If MiniMax M2.5 throughput degrades, swap to gpt-oss at Rung 2 (move gpt-oss → R2, MiniMax → R3).
- Qwen3-Next-80B is reserved as a strong fallback. If a slug churn removes one of the three active rungs, swap Qwen in.


## [2026-05-16] Bug — Orchestrator hardcodes model_id in JSONL regardless of actual model used

**What was discovered:**

After T05 added Opus/Haiku routing via _resolve_model in the Anthropic adapter, the orchestrator (orchestrator.py:102) still records model_id as f"{self.adapter.name}:{self._default_model}" — always "anthropic:claude-sonnet-4-6". A turn that actually routes to Opus via #opus tag or to Haiku via summariser hint is logged as if Sonnet answered. Routing itself works (verified by reading the adapter code); only the log is wrong.

**What was substituted / decided:**

Defer. The cosmetic logging bug doesn't break the cascade engine (T08) because no downstream code reads model_id back from the JSONL to make decisions. It does affect:
  - The acceptance report's evidence trail (T11 says "show that handovers picked the right model" — JSONL can't prove this until fixed)
  - The thread YAML's `models_used` aggregation (P1-T04's _aggregate_models) — currently builds incorrect coalesced runs

**Open for future review:**

Fix during T09 (orchestrator wiring) or T11 (acceptance). Adapter needs to surface the resolved model — either as a field on the final Token, or as a getter the orchestrator calls after streaming ends.

---

## [2026-05-23] P2a-T11 — Hot-reload bug: `_loop` always None at construction time

**What was discovered:**

`CascadeEngine.__init__` captured the asyncio event loop with `asyncio.get_running_loop()`. When `Orchestrator` is constructed inside `build_app()` (called from `__main__.main()` before `uvicorn.run()` starts), no event loop is running. So `_loop = None`. When watchdog fired `_on_config_changed`, the guard `if loop is None: return` silently dropped the reload request. Hot-reload appeared to work in unit tests (which mock or run inside async fixtures) but silently did nothing in the live server.

Confirmed by running a turn with the orchestrator started with a valid config, hot-reloading to a bad model name, and observing R1 still answering (no handover event — the bad config was never applied).

**What was substituted / decided:**

Two fixes applied:

1. **Lazy loop capture** (`cascade/engine.py`): Removed the eager `get_running_loop()` call from `__init__`. Added `_capture_loop_if_needed()` method, called at the start of every async entry point (`complete_stream`, `health_check_all`). On the first real request after server startup, the uvicorn event loop is running and gets captured correctly. Subsequent watchdog reloads use the stored reference.

2. **Broader file-event handling** (`cascade/config_watcher.py`): `_ChangeHandler` previously only handled `on_modified`. Added `on_created` (for atomic-write editors that produce a new inode) and `on_moved` (for write-then-rename patterns — common in VS Code and some CLI tools). Python `pathlib.Path.write_text()` on macOS triggers `on_modified` (in-place write), but this covers all editor patterns.

After both fixes: hot-reload confirmed working. Edit `cascade.yaml` mid-session → 1.5s later (500ms debounce + async scheduling) → next turn uses the new config.

**Open for future review:**

- The `_loop` reference becomes stale if the event loop is recreated (e.g., if uvicorn is restarted without restarting the Python process). This is not a realistic scenario for a single-process server but worth noting.
- Consider adding a startup log line `log.info("cascade watcher active; loop captured")` so operators can verify the watcher is functional.
- The hot-reload unit tests (`test_config_watcher.py`) test the watcher in isolation and do not exercise the loop scheduling path. Consider adding an integration-level test for `reload_config()` in an async context.

---

## [2026-05-23] P2a-T11 — R1 health check: `claude --version` times out under cold LaunchAgent

**What was discovered:**

The LaunchAgent sets a restricted PATH. The `claude` binary is a shim at `/opt/homebrew/bin/claude` that initialises brew/NVM on first invocation. Under the LaunchAgent's cold environment, the first `claude --version` call exceeds the 5-second `health_check` timeout. The second and subsequent calls are fast (~0.3s) because the shim has cached its initialisation. This causes the `/health` endpoint to briefly report R1 unhealthy at server startup, even though completions work correctly on the very first turn.

Confirmed via: `curl /health` immediately after `launchctl load` → R1 sometimes unhealthy. Same curl 5s later → R1 healthy. Meanwhile turns streamed successfully throughout.

**What was substituted / decided:**

Documented, not fixed. The user noted this as a known cosmetic issue in the T11 pre-flight brief. No cascaded failovers were triggered by the transient unhealthy report.

**Open for future review:**

- Increase the `health_check` timeout in `AnthropicAdapter` from 5s to 15s (or make it configurable via `cascade.yaml`).
- Alternative: run a warmup `claude --version` call at orchestrator startup (asynchronously, before the first health check) to pre-warm the shim.
- The LaunchAgent plist could also add the full shim resolution path (e.g., `/Users/.../nvm/versions/node/.../bin`) to avoid the slow initialisation entirely, but this is fragile and maintenance-heavy.

---

## [2026-05-23] P2a-T11 — Live cascade observations: latency profile and quirks

**What was discovered:**

From the T11 live failover tests (R1 FAKE → R2 MiniMax M2.5 via OpenRouter free tier):

**Latency breakdown (R1→R2 failover path):**
- R1 Claude Code error response (FAKE model): 4–5s (Claude Code contacts Anthropic API, gets 404/model-not-found, returns synthetic error event)
- Haiku handover doc generation: 3–5s (includes JSONL write, projection, Handovers/ write)
- MiniMax M2.5 first token (OpenRouter free tier): 8–12s (MiniMax is a reasoning model; generates reasoning tokens before content)
- **Total failover-to-first-MiniMax-token: ~15–22s**

This is acceptable for a failover scenario (the user already waited on R1), but would be noticeable if failover happens frequently. Phase 4 quota tracking should help predict and reduce failover frequency.

**Claude Code synthetic error token (HARD_FAIL path):**
When `claude --model <invalid>` is invoked, Claude Code emits a synthetic `assistant` event with model `"<synthetic>"` containing the human-readable error text before emitting `result.is_error=true`. The adapter yields this as a Token (not filtered as an error). Users see the error message inline ("There's an issue with the selected model...") before the handover divider and R2's response. This is arguably correct behaviour (the error is informative) but looks noisy in the plugin transcript.

**Open for future review:**
- Consider filtering/suppressing the synthetic error token from the plugin transcript, or styling it differently (smaller font, muted colour) to distinguish it from real content.
- The error text contains actionable advice ("Run --model to pick a different model") that makes no sense for users who don't control the model string. A Phase 2b cleanup pass could intercept this pattern and replace it with a friendlier in-plugin message.
- The 8–12s MiniMax first-token latency is the biggest driver of failover UX pain. If MiniMax gets slower or DeepSeek V4 Flash is faster, consider reordering R2/R4.
- Re-verify all three OpenRouter slugs at Phase 2b start (free-tier models appear and disappear).

---

## [2026-05-23] P2b-T03 — Web search tool findings

**What was discovered:**

1. Tavily was not used. The implementation uses DuckDuckGo via the `ddgs` library (no API key required). Tavily would require signing up for a key; ddgs is zero-config. DuckDuckGo results are adequate for general web search; Tavily would be higher quality. Recorded here so Phase 3 can revisit if search quality becomes a concern.

2. The SDK streaming path (`_complete_stream_sdk`) requires `ANTHROPIC_API_KEY` as an environment variable — it cannot reuse the Claude Code subscription auth that the subprocess path uses. This means web tools only work when an Anthropic API key is present in the environment. Users on the Pro subscription who want web tools must also generate an API key at console.anthropic.com and add it to their .env. The subprocess path continues to work without it.

**Open for future review:**

- Add ANTHROPIC_API_KEY to .env and LaunchAgent plist before running Phase 2b acceptance (T08).
- Evaluate Tavily as a higher-quality search alternative if DuckDuckGo results prove inadequate.
- The two auth paths (subprocess via Claude Code subscription vs. SDK via API key) mean the adapter has two different billing surfaces. Phase 4's quota tracking needs to account for both.

---

## [2026-05-23] P2b-T08 — Web tools confirmed blocked by paid API key requirement

**What was discovered:**

`ANTHROPIC_API_KEY` was empty in the LaunchAgent plist at Phase 2b acceptance. With
`enable_web_tools: true` in cascade.yaml, the SDK path is taken for all Rung 1 turns
(`complete_stream` → `_complete_stream_sdk`). The SDK path checks for `ANTHROPIC_API_KEY`
before model resolution and raises `AdapterUnavailable` when absent, causing a `HARD_FAIL`
cascade to Rung 2. All web search tests (P2b-T03/T04/T05 live path) and Haiku summariser
calls were blocked by this.

The Haiku summariser is also affected: stderr log shows `"Haiku summariser failed
(enable_web_tools=True requires ANTHROPIC_API_KEY); using fallback template."` Handover
doc summaries degraded to template text.

**What was substituted / decided:**

Live web-tools tests deferred. Unit tests (SDK path, registry, web_fetch, cascade events)
all pass and verify the implementation is correct.

**Open for future review:**

- Phase 2c: Add proactive health check — if `enable_web_tools=True` and `ANTHROPIC_API_KEY`
  is absent, mark Rung 1 unhealthy at startup rather than failing at first turn.
- Phase 2c: Haiku summariser should bypass the `enable_web_tools` path — it does not need
  web search. Fix: skip the SDK branch when `selection_hint == "summariser"`, or add a
  `summariser_use_web_tools: false` config option.
- The paid API key is fundamentally separate from the Pro subscription Claude Code auth.
  Two billing surfaces (subscription turns vs API key turns). Phase 4 quota tracking must
  account for both.

---

## [2026-05-23] P2b-T08 — Model pill label doesn't refresh after pinning

**What was discovered:**

After a pin is selected from the model picker menu (`pinToRung()`), the JSONL `thread_pin`
event is confirmed (backend registered the pin), and the menu checkmark updates correctly.
However the pill label in the chat header — which shows `R{n} ModelName` — does not update
until the next `renderModelPill()` call, which only happens on `handover` SSE events or
the next turn's token stream.

Root cause: `pinToRung()` sets `this.pinnedRung = rung` but the existing code calls
`this.renderModelPill()` immediately after. The pill style (pin outline) updates, but
`this.activeModel` is not changed — so the label still reflects the *current active* rung,
not the newly pinned one. This is a visual inconsistency: the outline and the checkmark say
"pinned to R2" but the label still says "R1 Sonnet 4.6".

**What was substituted / decided:**

Deferred. The backend pin is correct; this is display-only.

**Open for future review:**

- Phase 2c one-liner: in `pinToRung()`, after setting `this.pinnedRung`, also update
  `this.activeModel = { rung, model: slug }` before calling `this.renderModelPill()`.
  This will immediately reflect the pinned rung in the pill label. Only do this if the
  thread's *current* active model should visually jump to the pinned one on pin — the UX
  intent should be confirmed first.

---

## [2026-05-23] P2b-T08 — Opus heuristic wiring bug (live orchestrator, deferred)

**What was discovered:**

The auto-escalation heuristic (`OpusHeuristic`) was implemented in `heuristics/opus.py`,
unit-tested (17 tests pass), and wired into `_resolve_model()` in `AnthropicAdapter`.
However, in the live orchestrator the heuristic does not fire — long messages containing
complexity keywords still route to Sonnet rather than Opus.

Root cause: a wiring bug in the cascade engine's `_instantiate_adapter` path. The
`OpusHeuristicConfig` is read from cascade.yaml and used to construct the adapter, but
the heuristic object is not being correctly passed through to the adapter instance in the
live code path. Unit tests mock the adapter directly and therefore pass regardless.

**What was substituted / decided:**

Heuristic disabled in practice for Phase 2b. Bug deferred to Phase 2c.

**Open for future review:**

- Phase 2c: Audit `cascade/engine.py::_instantiate_adapter` — confirm `opus_heuristic=`
  kwarg is being passed correctly, that `OpusHeuristicConfig` is parsed from the YAML
  `rung.opus_heuristic` block, and that `enabled: true` in cascade.yaml reaches the
  `OpusHeuristic.__init__` with `_enabled = True`.
- Add an integration-level test that constructs the full engine from a fixture cascade.yaml
  with `opus_heuristic.enabled: true` and verifies a long keyword-rich message resolves
  to `claude-opus-4-7` end-to-end through the engine (not just through the adapter alone).

---

## [2026-05-30] P2c-T01 — Quota keyword "limit" too broad; 5xx now AdapterRateLimit

**What was discovered:**

The ticket spec listed `"quota"/"daily"/"limit"` as the three keywords that distinguish
`AdapterQuotaExhausted` from `AdapterRateLimit` on HTTP 429. The word `"limit"` alone is
ambiguous: `"rate limit exceeded"` (a per-minute throttle) contains `"limit"` and would be
misclassified as quota exhaustion.

Also: the ticket spec changes 5xx from `AdapterUnavailable` (old behavior) to
`AdapterRateLimit(cooldown_seconds=30)` (treat as transient, brief cooldown). The existing
test `test_server_error_5xx_raises_adapter_unavailable` was updated to match.

**What was substituted / decided:**

- Quota classifier uses only `"quota"` and `"daily"` keywords (not bare `"limit"`).
  Real OpenRouter daily-limit messages include "daily" or "quota"; per-minute throttle
  messages do not. The existing test fixtures ("daily quota limit exceeded") still match.
- 5xx → `AdapterRateLimit(cooldown_seconds=30)` as specified. The `triggers.py`
  `SOFT_DEGRADE` branch picks this up automatically for cascade decisions.

**Open for future review:**

- `AdapterModelUnavailable` is not yet handled in `triggers.py` — T03/T04 will add that.
  Until then it propagates as an unhandled exception in the cascade engine.
- The `AdapterRateLimit.cooldown_seconds` attribute is not yet consumed by `triggers.py`
  (it uses a hardcoded 30s). T03 should read it to get per-model adaptive cooldowns.

## [2026-05-30] P2c-T06 UI polish — routing panel overlaps transcript
The routing status floating panel renders with correct glass styling and accurate per-model health, but it overlays the chat transcript text rather than sitting cleanly above it. Minor visual issue. Fix in a T06 polish pass: raise z-index, ensure opaque-enough backdrop, or anchor below the pill so it doesn't overlap messages. Functional behavior is correct; cosmetic only.

---

## [2026-05-29] P2c-T01 — Dead-provider misclassified as rate-limit (incident + root cause)

**What was discovered:**

On 2026-05-29, the live test showed `deepseek-v4-flash`, `qwen3-next`, and several other pool members returning HTTP 404 / "Provider returned error" from OpenRouter. The pre-T01 adapter's error classifier mapped any HTTP 4xx to `AdapterRateLimit`, which:
- Applied a short per-model cooldown (30 s) and reported "briefly rate-limited" to the user.
- Never marked the model dead in the health ledger (a rate-limit is transient; model-unavailable is not).
- Led to the dead model being retried on the very next turn, repeating the failure.

The user saw misleading "rate-limited" messages for models that had been removed from their providers entirely.

**What was substituted / decided:**

T01 introduced `AdapterModelUnavailable` as a distinct exception (separate from `AdapterRateLimit` and `AdapterQuotaExhausted`). The classifier now routes:
- HTTP 404, or body containing "provider returned error" / "model not found" → `AdapterModelUnavailable`.
- HTTP 429 with "quota" or "daily" keywords → `AdapterQuotaExhausted`.
- HTTP 429 without quota keywords → `AdapterRateLimit`.
- HTTP 5xx → `AdapterRateLimit(cooldown_seconds=30)` (transient; no health mark).

`triggers.py` maps `AdapterModelUnavailable` → `model_unavailable` handover reason → marks the model dead in `ModelHealth` with exponential backoff. The regression is covered by `test_error_classification_accurate` in the Phase 2c integration suite.

**Open for future review:**

- Monitor OpenRouter for patterns where a model temporarily returns 404 during provider maintenance but recovers within hours. If this becomes common, consider a shorter first-failure backoff (1 min) before the 5 min → 15 min → 60 min schedule.
- The bare `"limit"` keyword was removed from the quota classifier (only `"quota"` and `"daily"` now match). If a new provider uses a message that triggers this path incorrectly, adjust the keyword list.

---

## [2026-05-31] P2c-T08 — Two-copy split-brain consolidated; canonical paths locked

**What was discovered:**

During the T08 run, it was confirmed that the project had been split across two local paths:
- `~/Documents/nexus/` — the active code repository (git-tracked, LaunchAgent-pointed).
- `~/nexus/` (home directory copy) — a stale duplicate from an earlier setup, no longer active.

Running the orchestrator from the home copy would have used a different vault path and potentially a different `cascade.yaml`, producing silent divergence in `model_health.json` state and JSONL logs.

**What was substituted / decided:**

- **Code lives at:** `~/Documents/nexus/nexus` (this repo).
- **Vault lives at:** `~/nexus-test-vault` (Obsidian opens this; orchestrator writes here).
- **LaunchAgent plist** was repointed to `~/Documents/nexus/nexus` as the working directory.
- The `~/nexus` home copy is abandoned and must never be re-introduced.

**Open for future review:**

- Never create a copy of the orchestrator source under `~/nexus` or any other home-directory path. If the project moves, update the LaunchAgent plist explicitly and verify `ps aux | grep orchestrator` shows the correct path before running acceptance tests.
- The LaunchAgent plist path (`~/Library/LaunchAgents/com.nexus.orchestrator.plist`) is the authoritative reference for what binary runs at login.

---

## [2026-05-31] P2c-T08 — route_by_role ships false; flip-on criteria

**What was discovered:**

The task classifier (T07) is logging `turn_classified` events correctly with `task_class` ∈ {coding, conversational, research} for every turn. The `route_by_role` flag ships as `false` in the default `cascade.yaml`. With `route_by_role=false`, classification decisions have no effect on model selection — the pool still routes by tier as usual.

**What was substituted / decided:**

Shipping with `route_by_role=false` is intentional. The heuristic classifier hasn't been validated against real usage yet. Routing on an un-tuned classifier could route coding turns to `qwen3-coder` when `nemotron-super` (broader and stronger) would have been better.

**Open for future review:**

Flip-on criteria for `route_by_role: true`:
1. Accumulate ≥ 50 turns of `turn_classified` JSONL across normal usage.
2. Manually review the classifications: are coding turns actually classified as coding? Are research turns being detected correctly vs. conversational?
3. If accuracy looks reasonable (>85% eyeballed), flip `route_by_role: true` in `cascade.yaml` and monitor for unexpected model selections.
4. Do not flip on until deepseek-v4-flash and qwen3-coder have been live and stable for at least one soak period (they were dead at Phase 2c acceptance).


## [2026-05-31] Web search already works on rung 1 via subprocess — paid SDK path was never needed
Verified live: sending a current-info question through the orchestrator on rung 1 (Claude Code subprocess, Pro subscription) triggers autonomous web search with source citations — confirmed with "latest Claude model" returning Opus 4.8 (2026-05-28) and four source links. No ANTHROPIC_API_KEY, no cost.

Implication: the Phase 2b web-tools work (SDK path, enable_web_tools flag, gated on paid ANTHROPIC_API_KEY) was a parallel mechanism that was never necessary for rung-1 web access. The subprocess has WebSearch on by default.

Action items (low priority):
- Surface a search indicator (🔍) in the chat UI when a subprocess turn performs a web search, so it's visible rather than inferred. Could fold into the T06 routing-panel polish pass.
- Consider deleting the dead paid SDK web-tools path (enable_web_tools, _complete_stream_sdk) to remove confusion, OR repurpose it later for pool-model web access (OpenRouter models don't get subprocess web search).
- Note: this only covers rung-1 (Anthropic) turns. OpenRouter pool models do NOT have web access; that would need per-provider tool wiring (future).

---

## [2026-07-03] Phase 3W M1 — Pool refresh, live-probed July 2026 free-tier models

**Method:**

Queried `https://openrouter.ai/api/v1/models` (340 models, 26 with prompt==0 AND completion==0). Behaviorally probed 14 candidates: one completion each, `max_tokens=500`, prompt "Explain in 100 words why monads are useful in functional programming." Recorded latency, finish_reason, whether reasoning sits in the separate `reasoning` field or bleeds into `content`, and whether the model actually answered.

**Not on the free tier anymore (mission shortlist casualties):** `minimax/minimax-m2.5` (Rung-2 workhorse since Phase 2a), `deepseek/deepseek-v4-flash`, `moonshotai/kimi-k2.6`, `google/gemma-4` non-it variants. MiniMax's exit confirms the May warning that free-tier availability is volatile.

**Probe table (2026-07-03):**

| Slug | Latency | Finish | Reasoning behavior | Answered? | Verdict |
|---|---|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 14.6s | stop | clean separate field (446 ch) | ✓ | **Pool: frontier** (reasoning, general) |
| `nvidia/nemotron-3-super-120b-a12b:free` | 9.6s | stop | clean separate field (912 ch) — bled in May 2026 probe | ✓ | **Pool: frontier** (reasoning, general) + `reasoning_mode: exclude` belt-and-braces |
| `openai/gpt-oss-120b:free` | 0.7s | — | — | provider error | **Pool: frontier** dead-at-probe; strong when live (health routing handles) |
| `qwen/qwen3-coder:free` | 0.3s | — | — | provider error | **Pool: strong** (coding) dead-at-probe |
| `poolside/laguna-m.1:free` | 0.3s | — | — | provider error | **Pool: strong** (coding) dead-at-probe |
| `qwen/qwen3-next-80b-a3b-instruct:free` | 0.3s | — | — | provider error | **Pool: strong** (general) dead-at-probe |
| `google/gemma-4-31b-it:free` | 3.7s | stop | no reasoning tokens at all | ✓ | **Pool: reliable** (general, fast) — fastest clean answer |
| `poolside/laguna-xs.2:free` | 5.1s | stop | clean separate field (1630 ch) | ✓ | **Pool: reliable** (coding, fast) — new July slug |
| `poolside/laguna-xs-2.1:free` | 7.7s | stop | clean separate field (1411 ch) | ✓ | **Pool: reliable** (coding, fast) |
| `openai/gpt-oss-20b:free` | 7.3s | length | reasoning consumed the entire 500-token budget, content EMPTY | ✗ | **Pool: reliable** (general, fast) + `reasoning_mode: exclude` (required, not optional) |
| `google/gemma-4-26b-a4b-it:free` | 233s | — | — | connection lost | Drop — flaky, weaker sibling of 31b which is live |
| `cohere/north-mini-code:free` | 6.0s | length | reasoning 2392 ch, content 43 ch | ✗ | Drop — reasoning starves output, weak |
| `meta-llama/llama-3.3-70b-instruct:free` | 0.3s | — | — | provider error | Drop — dead AND dominated by pool members |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 2.8s | length | **BLEEDS** — thought monologue duplicated into `content` | ✗ | Drop — bleeder |

**What was substituted / decided:**

10-member pool written to the vault `cascade.yaml` (frontier: nemotron-ultra, nemotron-super, gpt-oss-120b · strong: qwen3-coder, laguna-m.1, qwen3-next-80b · reliable: gemma-4-31b, laguna-xs.2, laguna-xs-2.1, gpt-oss-20b). Rung-1 Anthropic block and Ollama floor unchanged. Four of ten members were dead at probe time — pooled anyway per Phase 3W policy: the Phase 2c health ledger + backoff handles dead members, and they're strong when providers recover.

`reasoning_mode: exclude` set on nemotron-3-super (May bleed history; today's probe was clean — one clean probe doesn't erase a bleed record) and gpt-oss-20b (reasoning starves the completion budget without it).

**Note on a prior-session partial:** an uncommitted 2026-07-02 pool edit existed in the vault cascade.yaml (same direction, 9 members, no probe table recorded). This entry supersedes it; the 2026-07-03 re-probe is the evidence of record.

**Open for future review:**

- Re-probe at every phase boundary; free-tier churn is now proven (MiniMax M2.5 lasted ~6 weeks).
- gpt-oss-20b with `reasoning_mode: exclude` unverified live at high token budgets — if it still returns empty content on real turns, drop it.
- `poolside/laguna-xs.2` vs `laguna-xs-2.1` versioning is opaque; if poolside publishes an ordering, keep only the newer.
- The probe found `openrouter/free` (a meta-router slug, 200K ctx). Deliberately not pooled — it hides which model answers, which breaks the health ledger and the UI's model pill.

---

## [2026-07-03] Phase 3W acceptance — hosting decision record + decisions bundle

**Hosting decision (the Phase 3W pivot):** The dashboard is a web app served by the
orchestrator itself at `http://127.0.0.1:7777/app` — plain HTML/CSS/JS from
`nexus/orchestrator/webapp/`, no build step, no framework. The Obsidian plugin is
frozen as legacy chat: it keeps working against the same backend and gained exactly
one command ("Open Nexus Home"), but no dashboard features will be ported into it.
New product surface = the web app. Rationale: no plugin-runtime CORS/preflight
class of bugs, native iframes for Phase 3B live previews, one styling system we
fully control.

**Token-templating tradeoff:** `GET /app` templates the bearer token directly into
the served page (`window.NEXUS_TOKEN`). Accepted because the server binds
127.0.0.1 only on a single-user machine. Consequences: any local process that can
GET the unauthenticated /app page can read the token — equivalent to reading
`~/nexus-test-vault/Orchestrator/Config/auth.token`, which any local process could
already do. If the server ever binds beyond loopback, this MUST change to a login
flow or OS-keychain handshake before shipping.

**auto-mode supersedes route_by_role:** The Phase 2c `classifier.route_by_role`
flag never shipped enabled and is now superseded by mode-based routing: `/turn`
takes `mode: auto|light|hard|research`; auto maps classifier output
(conversational/general→light, coding→hard w/ coding-role bias, research→research).
The flag is still parsed (backward compat) but ignored, with a load-time warning.
The 2026-05-31 "flip-on criteria" entry is obsolete — classification now routes by
default via auto mode's tier policy, with pin > health > role-preference invariants
enforced in the engine and covered by TestModePolicy.

**Pool snapshot date:** 2026-07-03 (see the M1 probe-table entry above). Re-probe
at every phase boundary; free-tier churn is proven (MiniMax M2.5 lasted ~6 weeks).

**Behavioral note:** default `mode:"auto"` means short conversational turns now go
to the free pool instead of Sonnet — the intended quota-preserving change, and it
applies to the plugin's chat too (its payload sends no mode → auto).

**Deferred:**
- Obsidian live regression turn (Obsidian wasn't running at acceptance; SSE
  contract verified backward-compatible — new `meta` event is ignored by the
  plugin's explicit-match dispatcher).
- Human visual pass of the dashboard in light and dark.
- gpt-oss-20b with `reasoning_mode: exclude` unobserved on long live turns.
- Phase 3B scope unchanged: run commands, start/stop with streamed output, live
  iframe previews (natively possible now that the dashboard is a web page).

---

## [2026-07-03] Phase 3B — Add-flow fix, project start/stop, live model sidebar

**What was discovered:**

1. **CSS bug: `.chat-panel[hidden]` was not actually hidden.** `style.css`'s `.chat-panel` rule set `display: flex` unconditionally. Author-stylesheet rules always beat the UA stylesheet's `[hidden] { display: none }` default regardless of source order or specificity tiebreaks, so the chat panel rendered fully visible and pointer-event-active on every page load, `hidden` attribute notwithstanding. Confirmed via a headless-Chromium (`playwright-core` against the system Chrome binary — no `chromium-cli` or bundled browser was available in this environment) click on "Add Projects" that timed out with Playwright reporting `<aside hidden id="chat-panel"> intercepts pointer events`. This explains reports of dashboard buttons "not doing anything" depending on layout/window size. Fixed with an explicit `.chat-panel[hidden] { display: none; }` override (higher specificity via the attribute selector, wins regardless of order). Applied the same explicit-override discipline to the new `.model-sidebar[hidden]` rule proactively so the sidebar doesn't regress the same way.

2. **The "Add Projects" button and its discover→review→approve flow already existed** in `app.js` from the Phase 3W M3 session (button, `openAddFlow()`, `POST /projects/discover`, per-slug `POST /projects/index`). It was never actually broken — bug #1 above is the most likely explanation for why it appeared non-functional. Re-verified end-to-end live: opened the modal, saw 3 real un-indexed candidates, approved "Sample project," watched it get a real Sonnet summary and appear as a new wall card. Zero console errors.

3. **`/stop` had a real state-accuracy race.** `asyncio.subprocess.Process.returncode` is only set once something `await`s `proc.wait()`; the initial implementation had `stop()` call `proc.terminate()` and immediately return `status()`, which still read `returncode is None` (the output-pump task hadn't reached `EOF → wait()` yet) — so the HTTP response said `"running": true"` for a process that had just been sent SIGTERM. Caught live: the web UI's Stop button clicked correctly but didn't flip to "Start" because it trusted the (stale) response. Fixed by making `ProjectRunner.stop()` async and awaiting `proc.wait()` (with a 5s timeout, escalating to SIGKILL, then a further 2s wait) before returning — the response is now always accurate. `Orchestrator.stop_project` and the `POST /projects/{slug}/stop` route both became async as a result.

**What was substituted / decided:**

- **Process-state-not-persisted tradeoff (explicit, by design):** `ProjectRunner`'s `dict[str, _RunningProcess]` lives only in the orchestrator's process memory. Restarting the orchestrator (LaunchAgent reload, crash, `launchctl unload`/`load`) silently loses track of any child process it started — the child keeps running (orphaned, no longer visible to `/status`) until it exits or is killed manually. This was accepted as the minimal v1 shape per the ticket ("not persisted"). No PID file, no reconciliation-on-startup, no orphan sweep.
- One process per project slug, enforced by the runner dict; starting twice is idempotent (returns current status, no double-spawn); stopping twice is idempotent (no-op past the first call).
- Output is stdout+stderr merged (`stderr=STDOUT`) into one 200-line ring buffer (`collections.deque(maxlen=200)`), decoded permissively (`errors="replace"`).
- `pool_health_status()` (engine.py) now annotates each pool member's status dict with `tier` (zipped from `PoolConfig.members`, same order as `all_status()`'s input) — additive, non-breaking — so the new live model sidebar can group/label by tier without a second endpoint.

**Open for future review:**

- No orphan-adoption on orchestrator restart. If this becomes a real pain point (e.g., you frequently restart the orchestrator while dev servers are running), the fix is a small PID-file-per-slug scheme in `Orchestrator/Config/` plus a startup reconciliation pass that re-attaches to still-alive PIDs (best-effort; output history from before the restart would still be lost since the ring buffer is in-memory only).
- `stop()`'s SIGKILL escalation (5s SIGTERM grace, 2s SIGKILL grace) is untuned — fine for a dev-server heartbeat script; unverified against a process that traps SIGTERM and does slow cleanup.
- The model sidebar polls `/health` every 7s independent of the chat panel's own routing popover (same data, two independent pollers). Harmless (health_check is cheap, no completion quota used) but could be unified into one shared poll + pub/sub if more panels end up wanting live health data.
- No project skill exists yet for launching/driving this web app; `/run-skill-generator` would be worth running given `playwright-core` + the system Chrome binary is now a proven, repeatable recipe for this repo (documented inline in this session's throwaway scripts, not yet captured as a skill).

---

## [2026-07-03] Phase 3B follow-up — chat-close bug report did not reproduce; hardened the [hidden] pattern anyway

**What was investigated:**

A bug report claimed the chat panel's close button (`#chat-close`) didn't hide the panel when clicked in a browser, no console errors. Extensive live reproduction via headless Chrome (`playwright-core` + system Chrome, per the established recipe) across every scenario constructible:

- Fresh open → close (DOM/computed-style inspection, not just visual/console-error absence)
- Double open/close cycles
- Chat panel and model sidebar open simultaneously (checked for geometric overlap — none at 1280px width; panels dock opposite sides)
- Raw pixel-coordinate mouse click (not Playwright's semantic `.click()`, in case real-mouse vs. synthetic-event dispatch differed)
- Close after sending a real chat turn (DOM populated with `.msg-meta`/`.handover-line` elements)
- Add Projects modal (z-index 60) opened on top of the chat panel (z-index 55) — confirmed via `elementFromPoint` that the scrim genuinely paints on top, Cancel closes only the modal, and the chat panel is untouched and still closable afterward

**All scenarios passed correctly** — `chat.panel.hidden` became `true`, the `hidden` attribute was present, `getComputedStyle(...).display` was `"none"`, zero console errors, single non-duplicated `#chat-close`/`#chat-panel`/`#chat-fab` IDs, no capturing/delegating listener anywhere in `app.js` that could intercept the click before the button's own `onclick`. Also ruled out a split-brain webapp copy (`OPEN_QUESTIONS.md`'s prior split-brain history) — searched the filesystem, found only an unrelated old `~/Downloads/nexus-fixed` (Phase-1 markdown tickets, no code) and confirmed the served `/app/static/{app.js,style.css}` hash-match the source tree exactly, single orchestrator process on port 7777.

**Conclusion:** the reported defect could not be reproduced against the current code. The most likely explanation is a browser tab/cache still holding the pre-fix JS/CSS from before the Phase 3B session's `.chat-panel[hidden]` override was deployed — StaticFiles serves with `ETag`/`Last-Modified` but no explicit `Cache-Control`, so a tab that was never reloaded (rather than one that was reloaded and got a stale cache hit) would show exactly this symptom.

**What was fixed anyway (hardening, not a reproduction):**

The per-element `.foo[hidden] { display: none }` override pattern introduced last session to fix the original chat-panel bug was fragile: it has to be remembered and added for every *new* panel that both uses the `hidden` attribute and sets `display` via a class (already needed twice — `.chat-panel` and `.model-sidebar`). Replaced both with one global rule near the top of `style.css`:

```css
[hidden] { display: none !important; }
```

This closes the entire bug class permanently — no future panel can silently un-hide itself via a `display` declaration, `!important` on a plain attribute selector beats any non-`!important` author rule regardless of specificity. The two now-redundant per-element overrides were removed.

**Open for future review:**

- If the close-button symptom recurs, first ask "did you hard-refresh (Cmd+Shift+R)?" before assuming a code regression — this session's evidence points strongly at stale-tab caching, not a live defect.
- Consider adding `Cache-Control: no-cache` (forces revalidation every load) to the `/app/static` `StaticFiles` mount if stale-asset confusion recurs during active development. Not done now — low value while the ETag/Last-Modified conditional-request behavior is already correct.
