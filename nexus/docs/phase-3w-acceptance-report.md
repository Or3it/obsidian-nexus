# Phase 3W Acceptance Report — "Nexus Home"

Date: 2026-07-03
Tester: Claude Code (autonomous session) — live curls against the running orchestrator
Environment: macOS Darwin 25.5.0 | Python 3.12 | Claude Code CLI (rung 1, Pro subscription)
Orchestrator: ~/Documents/nexus/nexus (LaunchAgent com.example.nexus) Vault: ~/nexus-test-vault

## Scope delivered

| Milestone | Commit | Status |
|---|---|---|
| M0 preflight | 188874c | ✓ |
| M1 pool refresh (live-probed July 2026 models) | 8a97fb8 | ✓ |
| M2 modes + project endpoints + Nexus voice + SSE meta | 880f164 | ✓ |
| M3 Nexus Home web dashboard at /app | d989288 | ✓ |
| M4 Obsidian handoff | 7bc89dd | ✓ |
| M5 acceptance (this report) | — | ✓ |

## 1. pytest full green

```
674 passed, 22 skipped (gated --real-providers) in ~12s
```

Up from the 634-test baseline at M0. ruff + mypy clean at every commit.
New coverage: mode policy matrix (ordering per mode with mocked health states),
pin-beats-mode, health-beats-role-preference, Opus-never-auto, research fallback
notice, resolved-mode threading to adapters, meta-event emission and JSONL
model_id recording, voice injection on both adapter paths (subprocess argv and
OpenRouter wire payload), project digest on/off, detail endpoint (incl. fresh
re-read), /app page + static assets.

## 2. Pool probe (2026-07-03, full table in OPEN_QUESTIONS.md)

26 of 340 OpenRouter models are free. 14 probed behaviorally (one completion,
max_tokens=500, same prompt). Off the free tier since May: **minimax-m2.5,
deepseek-v4-flash, kimi** — the volatility warning proved out.

Final 10-member pool (vault cascade.yaml; rung-1 Anthropic + Ollama floor unchanged):

| Member | Tier | Roles | Probe |
|---|---|---|---|
| nvidia/nemotron-3-ultra-550b-a55b:free | frontier | reasoning, general | 14.6s, clean reasoning field, answered |
| nvidia/nemotron-3-super-120b-a12b:free | frontier | reasoning, general | 9.6s, clean (bled in May → `reasoning_mode: exclude`) |
| openai/gpt-oss-120b:free | frontier | reasoning, coding, general | dead-at-probe; pooled (health routing) |
| qwen/qwen3-coder:free | strong | coding | dead-at-probe; pooled |
| poolside/laguna-m.1:free | strong | coding | dead-at-probe; pooled |
| qwen/qwen3-next-80b-a3b-instruct:free | strong | general | dead-at-probe; pooled |
| google/gemma-4-31b-it:free | reliable | general, fast | 3.7s, no reasoning tokens — fastest clean |
| poolside/laguna-xs.2:free | reliable | coding, fast | 5.1s, clean |
| poolside/laguna-xs-2.1:free | reliable | coding, fast | 7.7s, clean |
| openai/gpt-oss-20b:free | reliable | general, fast | reasoning starved 500-tok budget → `reasoning_mode: exclude` |

Dropped: nemotron-3-nano-30b (reasoning bleeds into content), cohere/north-mini-code
(reasoning starves output, weak), llama-3.3-70b (dead + dominated),
gemma-4-26b-a4b (233s connection loss, weaker sibling live), `openrouter/free`
meta-router (hides which model answers → breaks health ledger + model pill).

## 3. Mode matrix — verified LIVE (curl transcripts, 2026-07-03)

**light** — `"hi"` → SSE `meta {"mode":"light","model":"poolside/laguna-xs.2:free"}`;
JSONL proof: `turn_classified {"task_class":"conversational","mode":"light"}` and
`turn_assistant {"model_id":"poolside/laguna-xs.2:free"}` — a free pool model, NOT Sonnet.
(In an earlier light turn gemma-4-31b was preferred first, errored upstream, and the
handover event + failover to laguna-xs.2 streamed correctly — health beats preference, live.)

**hard** — gnarly architecture question (JSONL schema migration, upcasting vs versioned
projections) → `meta {"mode":"hard","model":"claude-sonnet-4-6"}`; JSONL
`model_id: claude-sonnet-4-6`; structured deep answer.

**research** — "most recent Claude model as of this week?" →
`meta {"mode":"research","model":"claude-sonnet-4-6"}`. Reply opened with
"Let me check that with a live search rather than rely on my training data",
performed autonomous web search on the subprocess path, answered (Claude Fable 5,
2026-07-01; Sonnet 5, 2026-06-30) **with source links**. Voice + research preamble
both visible in behavior.

**auto** — coding message (buggy `def get_total` snippet) → classified `coding`,
resolved `mode: hard`, answered by `claude-sonnet-4-6` (JSONL proof).
Casual message ("good evening!") → classified `conversational`, resolved
`mode: light`, answered by `poolside/laguna-xs.2:free` (JSONL proof).

**Invariants:** pin beats mode and health beats role preference are unit-tested
(`TestModePolicy`); Opus is never in any mode ordering (`test_opus_never_selected_by_any_mode`)
and remains manual-`#opus`-only in the adapter; `turn_classified` now carries the
resolved mode on every turn. `route_by_role` is superseded — parsed for backward
compat, ignored by selection, warns at config load.

**Bonus, live:** `include_projects: true` + "what does sample-app do?"
→ free pool model answered correctly from the injected digest ("projects worship
lyrics in real time… Whisper/Qwen speech-to-text").

## 4. Dashboard checklist (Nexus Home at http://127.0.0.1:7777/app)

Verified via curl + API-level end-to-end (the UI calls exactly these endpoints):

- [x] `/app` serves the SPA (200, HTML, bearer token templated in — `{{NEXUS_TOKEN}}` absent);
      `/app/static/style.css` + `app.js` serve 200; `node --check` passes on app.js.
- [x] Wall renders indexed projects — `GET /projects` returns real notes
      (sample-app, sample-scraper) with summary/stack/status/category.
- [x] Add-flow indexes a real project end-to-end — `POST /projects/discover` returned
      6 un-indexed candidates; `POST /projects/index {"slugs":["sample-scraper"]}` produced a
      real Sonnet summary (category=scraping) and a `Projects/sample-scraper.md` note.
- [x] Detail view shows a real README — `GET /projects/sample-scraper/detail` returns the
      raw Next.js README (client-side markdown renderer displays it), 80-entry fresh
      file tree, git branch/dirty. Projects without a top-level README correctly
      return `readme: null` and the section is omitted.
- [x] VS Code handoff — `POST /projects/sample-app/open {"target":"vscode"}`
      → `{"ok":true}`, VS Code opened.
- [x] Remove deletes note not folder — `DELETE /projects/nexus` removed
      `Projects/nexus.md`; `~/Documents/nexus` untouched (verified on disk). The UI
      confirm dialog states this explicitly.
- [x] Chat streams with visible mode+model — SSE `meta` event carries
      `mode/requested_mode/model` (all transcripts above); handover reason lines and
      the routing popover are fed by the same `handover` + `/health pool[]` payloads
      verified live.
- [x] Light/dark: implemented via `prefers-color-scheme` with full dual token sets;
      backdrop-filter has an `@supports` solid-tint fallback; `prefers-reduced-motion`
      respected. Visual pass in both schemes is the one item left for human eyes.

## 5. Obsidian chat regression

Obsidian was not running during this session, so a live plugin turn could not be
sent. Contract-level verification instead:

- The plugin's SSE dispatcher (`views/chat-view.ts`) matches event types explicitly
  (`thread/token/handover/tool_call/tool_result/error/done`); the new `meta` event
  falls through and is ignored cleanly. No pre-existing event type or payload field
  changed — the stream is a strict superset.
- `POST /turn` new fields (`mode`, `include_projects`) are optional with defaults
  matching pre-3W behavior for the payload the plugin sends… with one deliberate
  change: default `mode: "auto"` now routes conversational turns to the light pool
  instead of Sonnet. That is the intended Phase 3W quota-preserving behavior, applies
  identically to plugin and web app, and every turn still cascades on failure.
- "Open Nexus Home" command added (Web Viewer if enabled, else system browser);
  `tsc --noEmit` clean; built `main.js` copied to the test vault.
- Follow-up: send one turn through the plugin next time Obsidian is open.

## Deferred / known gaps

- gpt-oss-20b's `reasoning_mode: exclude` is probe-verified but not yet observed on
  a long live turn; if it returns empty content in practice, drop it from the pool.
- Human visual pass of the dashboard in light + dark (all machine-checkable parts pass).
- Obsidian live regression turn (above).
- Phase 3B scope unchanged: per-project run commands, start/stop with streamed
  output, live iframe previews — the web pivot makes iframes natively possible.

## Sign-off

Pool refresh live-probed: ☑ Modes live-verified end-to-end: ☑
Voice on both adapter paths: ☑ Dashboard serving + full API loop: ☑
Obsidian handoff: ☑ (live turn deferred) 674 tests green: ☑

Phase 3W complete. Do not start Phase 3B without a new session brief.
