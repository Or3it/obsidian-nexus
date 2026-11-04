# P2a-T05 — Anthropic Adapter Extensions (Opus + Haiku Routing)

## Goal

The Phase 1 Anthropic adapter is hardcoded to Sonnet 4.6. Phase 2a needs it to support Opus 4.7 and Haiku 4.5 too — same provider, same auth, different model strings. Phase 2b will add the heuristic for when to use which. This ticket just makes the routing capability exist.

## Read first

- `nexus/orchestrator/adapters/anthropic.py` (the Phase 1 implementation)
- `shared/adapter-protocol.md` (especially the `selection_hint` field added in P2a-T01)
- `OPEN_QUESTIONS.md`

## What changes

Phase 1 today:

```python
class AnthropicAdapter:
    def __init__(self, model="claude-sonnet-4-6", ...):
        self.model = model
        ...

    def _build_command(self, request):
        return ["claude", "--model", self.model, "--print", "--output-format", "stream-json", "{prompt}"]
```

Phase 2a needs:

```python
class AnthropicAdapter:
    def __init__(
        self,
        default_model="claude-sonnet-4-6",
        premium_model="claude-opus-4-7",
        summariser_model="claude-haiku-4-5",
        ...,
    ):
        self.default_model = default_model
        self.premium_model = premium_model
        self.summariser_model = summariser_model
        ...

    def _resolve_model(self, request) -> str:
        """
        Decide which model to invoke:
          - If selection_hint == "summariser" → Haiku
          - If selection_hint == "premium" or message contains #opus → Opus
          - Otherwise → default (Sonnet)

        Phase 2a: only these explicit signals route to non-default. Phase 2b adds heuristic logic.
        """
        ...

    def _build_command(self, request):
        model = self._resolve_model(request)
        return ["claude", "--model", model, "--print", "--output-format", "stream-json", "{prompt}"]
```

## Files to modify

```
nexus/orchestrator/adapters/anthropic.py
nexus/tests/test_anthropic_adapter.py
```

No new dependencies.

## Implementation notes

1. **Three model strings, one adapter.** All three models authenticate the same way (Claude Code subscription auth). The adapter just picks which one to put in `--model`. No new subprocess logic.

2. **`selection_hint` semantics.** Per the cascade contract, the orchestrator/cascade passes a hint string. Recognized values for the Anthropic adapter:
   - `"summariser"` — use the configured summariser model (Haiku)
   - `"premium"` — use the configured premium model (Opus)
   - `"pin:claude-opus-4-7"` or `"pin:claude-haiku-4-5"` — explicit pin, use that exact model
   - `None` or anything else — use default

3. **`#opus` tag in the user's message.** If the last user message in `request.messages` contains the literal substring `#opus` (case-insensitive), route to Opus regardless of `selection_hint`. Strip the tag from the message content before sending (don't leak the routing signal into the model's input). This is the only "heuristic" Phase 2a ships — explicit user signal, not inferred behavior.

4. **`count_tokens` ratios differ per model.** Sonnet ~3.8 chars/token, Opus ~3.6 (the new 4.7 tokenizer is more verbose), Haiku ~3.9. Store as a dict, look up by active model.

5. **`available_context` differs per model.** Sonnet 200K, Opus 1M, Haiku 200K. Read from config.

6. **Cost estimation.** All three burn Pro quota at different rates:
   - Haiku: ~0.1× Sonnet rate
   - Sonnet: 1× baseline
   - Opus: ~3–5× Sonnet rate
   
   Phase 2a returns `pro_quota_pct: 0.0` still — actual quota tracking is Phase 4. But the structure should be in place to differentiate.

7. **Don't break Phase 1.** Existing tests must keep passing. The adapter's constructor signature changes (new params with defaults). All previous behavior remains the default.

## Tests

`tests/test_anthropic_adapter.py` — extend the existing test file:

- Default request (no hint, no `#opus`) routes to Sonnet — `_build_command` contains `--model claude-sonnet-4-6`.
- `selection_hint="summariser"` routes to Haiku.
- `selection_hint="premium"` routes to Opus.
- `selection_hint="pin:claude-opus-4-7"` routes to Opus.
- `selection_hint="pin:claude-haiku-4-5"` routes to Haiku.
- Last user message contains `#opus` → routes to Opus, and the `#opus` tag is stripped from the content sent to the subprocess.
- `#OPUS` (uppercase) also routes to Opus (case-insensitive).
- `#opus` in a *non-last* message (mid-conversation) does NOT trigger routing.
- `count_tokens` differs by active model (Opus longer than Sonnet for the same input).
- `available_context` differs by active model.
- Existing Phase 1 tests still pass (regression).

## Definition of done

- [ ] `anthropic.py` supports three models via the routing logic.
- [ ] `#opus` tag works and is stripped from user content.
- [ ] All Phase 1 + new tests pass.
- [ ] Manual smoke test:
  ```bash
  # Default Sonnet
  python -c "
  import asyncio
  from orchestrator.adapters.anthropic import AnthropicAdapter
  from orchestrator.types import NexusRequest, NexusMessage, StyleProfile
  a = AnthropicAdapter()
  r = NexusRequest(thread_id='t', messages=[NexusMessage(role='user', content='Say hello.')], style_profile=StyleProfile())
  print(asyncio.run(a.complete(r)))
  "
  
  # Opus via #opus tag
  python -c "
  ...
  r = NexusRequest(thread_id='t', messages=[NexusMessage(role='user', content='Say hello. #opus')], style_profile=StyleProfile())
  ...
  "
  ```
  Returns responses; the model_id in the response reflects which Claude was invoked.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't add auto-escalation heuristics. No "this looks like a hard question, use Opus." Phase 2b.
- Don't add the per-turn UI button to invoke Opus. Phase 2b extends the plugin for that.
- Don't track quota burn by model yet. Phase 4.
- Don't change the cascade.yaml schema beyond what's already there — `models: {default, premium, summariser}` already exists from P1-T02.
