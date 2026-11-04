# P2a-T11 — Integration + Acceptance

## Goal

Verify all eight Phase 2a acceptance criteria end-to-end against live providers (Anthropic, Google AI Studio, OpenRouter, local Ollama). Sign off Phase 2a so 2b can start.

## Read first

- `phase-2a/00-OVERVIEW.md` (the eight acceptance criteria)
- `phase-1/P1-T08-integration-and-acceptance.md` (template for this ticket's structure)

## Pre-flight

Before running this ticket:

- [ ] `GOOGLE_AI_STUDIO_API_KEY` exported in shell
- [ ] `OPENROUTER_API_KEY` exported in shell
- [ ] Ollama running with the configured model pulled
- [ ] `cascade.yaml` configured with all 5 rungs

Update the LaunchAgent plist to pass the API keys as environment variables (the LaunchAgent doesn't inherit your shell's env):

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>GOOGLE_AI_STUDIO_API_KEY</key>
    <string>YOUR_KEY_HERE</string>
    <key>OPENROUTER_API_KEY</key>
    <string>YOUR_KEY_HERE</string>
</dict>
```

Reload the agent:

```bash
launchctl unload ~/Library/LaunchAgents/com.example.nexus.plist
launchctl load ~/Library/LaunchAgents/com.example.nexus.plist
```

## Files to create

```
nexus/tests/integration/test_phase2a_e2e.py
nexus/docs/phase-2a-acceptance-report.md
```

## Integration tests

`tests/integration/test_phase2a_e2e.py`:

```python
import pytest
import httpx
import json
import time
from pathlib import Path


@pytest.mark.real_providers
class TestPhase2aE2E:
    """Live tests against Anthropic, Google AI Studio, OpenRouter, Ollama."""

    def test_rung_1_default_path(self, orchestrator_server, vault):
        """A normal turn streams from Sonnet (Rung 1)."""
        ...

    def test_rung_2_failover_when_rung_1_misconfigured(self, vault):
        """Force Rung 1 to fail by giving it a bad model string; verify Rung 2 takes over."""
        # Edit cascade.yaml to set rung 1 default to "claude-sonnet-NONEXISTENT"
        # Start orchestrator
        # Send a turn
        # Assert: SSE stream includes a handover event from R1 → R2
        # Assert: a handover doc exists in Handovers/
        # Assert: the response text is non-empty (Gemini answered)
        ...

    def test_rung_3_failover_when_rungs_1_and_2_fail(self, vault):
        """Both R1 and R2 misconfigured → cascade reaches R3 (GLM via OpenRouter)."""
        ...

    def test_rung_5_local_when_all_cloud_fail(self, vault):
        """All cloud rungs misconfigured → cascade reaches Rung 5 (local Ollama)."""
        ...

    def test_openrouter_quota_cools_both_3_and_4(self, vault):
        """Simulate OpenRouter quota exhaustion → both R3 and R4 are marked exhausted."""
        # Use a mock or a manual budgets.yaml edit
        ...

    def test_handover_doc_has_all_required_fields(self, vault):
        """Force a failover; the generated handover doc has all six required fields."""
        ...

    def test_hot_reload_picks_up_yaml_changes(self, vault):
        """Edit cascade.yaml while orchestrator is running; verify the change takes effect on next turn."""
        ...

    def test_pinned_model_overrides_cascade(self, vault):
        """Set pinned_model in a thread's YAML; verify that model is always used regardless of failover."""
        ...
```

## Manual acceptance report (`docs/phase-2a-acceptance-report.md`)

```markdown
# Phase 2a Acceptance Report

Date: ____
Tester: ____
Environment: macOS / Linux / Windows ____ | Python ____ | Ollama models pulled ____

## Eight acceptance criteria

- [ ] **1. `pytest tests/` passes.** `__ tests passed in __s` (excluding --real_providers).
- [ ] **2. All five adapters implement the protocol.** Files exist, conform to base.Adapter, per-adapter unit tests green.
- [ ] **3. Trigger taxonomy fires correctly.** All seven trigger classes have a passing unit test in test_triggers.py.
- [ ] **4. Handover docs generate.** Forced R1 failure produces a handover doc in `Orchestrator/Handovers/{id}_handover_01.md` with six required fields. *Sample attached.*
- [ ] **5. Continuation preserves context.** The handover doc accurately summarises the conversation up to that point. *Diff between manual summary and Haiku summary attached.*
- [ ] **6. Hot-reload works.** Edit cascade.yaml during a session; the next turn uses the new config without a restart. *Log excerpt attached.*
- [ ] **7. UI shows the active rung.** Pill colour/label changes on failover; toast appears; in-line divider in transcript. *Screenshot attached.*
- [ ] **8. Live test against real providers passes.** Manual failover R1 → R2 → R3 → R5 with real responses on each.

## Drift discovered (append to OPEN_QUESTIONS.md, paste new entries here too)

(paste contents of new OPEN_QUESTIONS.md entries)

## Soak observation

Ran 50 turns over __ hours mixing manual failovers, normal use, and config edits.
- Failover-to-first-token latency: P50 / P95: __ / __ s.
- Any crashes or unhandled errors: __
- Handovers that produced garbled or off-topic continuations: __
- Memory growth in orchestrator process over __ hours: __

## Sign-off

Phase 2a is ready for Phase 2b: ☐ Yes / ☐ No, blocking issues: __
```

## Definition of done

- [ ] Integration tests written; `--real_providers` flag gates them.
- [ ] All eight acceptance criteria green per the report.
- [ ] OPEN_QUESTIONS.md has any new findings from this run.
- [ ] One PR titled `feat(phase-2a): acceptance` containing the test file, the report, and any documentation updates.

## What goes in OPEN_QUESTIONS.md after this ticket

Expect to add entries about:

- Actual free-tier rate limits observed (vs documented).
- Handover quality at different failover depths (R1→R2 vs R1→R5).
- Any provider-specific quirks (Gemini thinking-mode hallucinations? GLM consistency?).
- Latency observations (which rungs are fast? which need timeout tuning?).

These shape Phase 2b's tickets — particularly the Opus heuristic and the web-tools design.
