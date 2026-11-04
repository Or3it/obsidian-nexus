# P2c-T07 — Task-Classifier Scaffold (Log-Only + Opt-In Role Routing)

## Goal

Lay the groundwork for the eventual "best model for the question type" vision without destabilizing health-aware routing. Build a classifier that tags each turn (`conversational` / `research` / `coding` / `general`), **log its decisions**, and make role-aware selection an **opt-in flag** that's off by default. This collects the real-usage data needed to tune true task routing in a later phase, and lets you A/B it safely.

## Why log-first, not route-first

Task routing only earns its keep if the classifier is accurate on *your* actual messages. Turning it on blind risks routing a quick question to a slow reasoning model or vice versa. So: classify every turn, log `{message_preview, predicted_class, model_used, latency, tier}`, and review after a few days. Flip the routing flag on once the logs show the classifier is right. This mirrors the "build an edge from data, then commit" principle.

## Read first

- T02 (PoolMember.roles), T04 (select_candidate)
- `nexus/orchestrator/adapters/ollama.py` (local classifier option)
- `nexus/orchestrator/heuristics/opus.py` (existing heuristic pattern to follow)

## Classifier design

Two backends, configurable:

1. **Heuristic (default, zero-cost):** keyword + structural rules. Fast, transparent, no model call.
   - `coding`: contains code fences, "function", "bug", "stack trace", "refactor", file extensions, "error:", language names
   - `research`: "explain", "compare", "analyze", "why", "history of", "tradeoff", long multi-sentence questions, "sources"
   - `conversational`: short (<120 chars), greetings, follow-ups without new nouns
   - else `general`

2. **Local-model (opt-in):** send the message to the Ollama floor model with a tiny classify prompt returning one word. Zero OpenRouter quota (runs locally). Slightly slower but more accurate. Used only if `classifier.backend: ollama`.

```yaml
classifier:
  enabled: true            # always classify + log
  backend: heuristic       # heuristic | ollama
  route_by_role: false     # OPT-IN — when true, selection prefers role-matching pool members
```

## Files to create / modify

```
nexus/orchestrator/heuristics/task_classifier.py   ← new
nexus/orchestrator/types.py                         ← ClassifierConfig
nexus/orchestrator/cascade/engine.py                ← classify each turn; log; opt-in role-bias in select
nexus/orchestrator/vault/event_log.py               ← new event type "turn_classified"
nexus/tests/test_task_classifier.py
```

## `task_classifier.py`

```python
from dataclasses import dataclass
from typing import Literal

TaskClass = Literal["conversational", "research", "coding", "general"]

@dataclass
class Classification:
    task_class: TaskClass
    confidence: Literal["high", "medium", "low"]
    reason: str

def classify_heuristic(message: str) -> Classification:
    text = message.lower()
    if ("```" in message or "def " in text or "function " in text
            or "stack trace" in text or "traceback" in text or "refactor" in text
            or any(ext in text for ext in (".py", ".ts", ".js", ".rs", ".go"))):
        return Classification("coding", "high", "code markers present")
    research_kw = ("explain", "compare", "analyze", "analyse", "why ",
                   "tradeoff", "trade-off", "history of", "sources", "evaluate")
    if any(k in text for k in research_kw) or message.count("?") >= 2 or len(message) > 400:
        return Classification("research", "medium", "research signals")
    if len(message) < 120:
        return Classification("conversational", "medium", "short message")
    return Classification("general", "low", "no strong signal")

async def classify_ollama(message: str, ollama_adapter) -> Classification:
    prompt = ("Classify this user message into exactly one word: "
              "conversational, research, coding, or general. Message: "
              f"\"{message[:500]}\". One word only.")
    out = (await ollama_adapter.complete_once(prompt, max_tokens=4)).strip().lower()
    cls = out if out in ("conversational", "research", "coding", "general") else "general"
    return Classification(cls, "medium", "ollama-classified")
```

## Engine integration

- After receiving a turn, run the classifier (heuristic or ollama per config).
- Append a `turn_classified` event to the JSONL: `{task_class, confidence, reason, backend}`.
- If `route_by_role: true`, bias `select_candidate`: among live candidates, prefer those whose `roles` include the predicted class's role mapping:
  - `coding` → roles containing `coding`
  - `research` → roles containing `reasoning`
  - `conversational` → Sonnet (rung 1) is already first; no change
  - `general` → no bias (tier order as normal)
  
  Role bias is a *preference within the live set*, never a reason to pick a dead model. Health always wins: a role-matching dead model is still skipped.
- If `route_by_role: false` (default), selection is unchanged from T04; the classification is logged only.

## Tests

`tests/test_task_classifier.py`:
- Code fence / "traceback" / ".py" → coding, high.
- "compare X and Y, why does..." / 2+ question marks / >400 chars → research.
- Short greeting → conversational.
- Bland medium statement → general, low.
- `classify_ollama` with mocked adapter returning "coding" → coding; returning garbage → general.

`tests/test_cascade_engine.py` (extend):
- `enabled: true, route_by_role: false` → `turn_classified` event logged, selection identical to T04.
- `route_by_role: true`, coding turn, coding-role frontier model live → that model preferred over a non-coding frontier model.
- `route_by_role: true`, coding turn, all coding-role models dead → falls back to tier order among live (health wins over role).

## Definition of done

- [ ] `task_classifier.py` with heuristic + ollama backends.
- [ ] `ClassifierConfig` in types.py; reads from cascade.yaml.
- [ ] Every turn logs a `turn_classified` event.
- [ ] `route_by_role` opt-in biases selection within the live set only; off by default.
- [ ] All tests pass.
- [ ] Manual: send a coding question and a casual question; confirm both get `turn_classified` events in the JSONL with sensible classes (routing flag can stay off).
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Do NOT make role routing the default. It ships off; you flip it on after reviewing logs.
- No multi-label or confidence-weighted routing yet — single class, simple bias.
- No per-class model overrides in config yet — role tags drive it. Explicit per-class pins are a later phase.
- This is the scaffold for full task routing, not the finished system.
