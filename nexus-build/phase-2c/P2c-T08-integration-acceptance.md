# P2c-T08 — Integration + Acceptance

## Goal

Verify all eight Phase 2c acceptance criteria end-to-end against live providers, then sign off Phase 2c.

## Read first

- `phase-2c/00-OVERVIEW.md` (the eight criteria)
- `docs/phase-2b-acceptance-report.md` (template + tone)

## Pre-flight

- [ ] `OPENROUTER_API_KEY` in `.env` and LaunchAgent plist
- [ ] `cascade.yaml` is schema-3 with the full pool + floor (backup the schema-2 file first)
- [ ] `model_health.json` path gitignored
- [ ] Orchestrator running from `~/Documents/nexus/nexus` against `~/nexus-test-vault` (post-consolidation paths), `/health` returns healthy
- [ ] Plugin is the T06 build (transparency UI)

## Integration tests

```python
@pytest.mark.real_providers
class TestPhase2cE2E:

    def test_strongest_live_model_selected(self, server, vault):
        """With the real pool, a turn routes to a live frontier model (JSONL model_id
        is one of the frontier tier), no manual slug edits."""

    def test_dead_model_detected_and_skipped(self, server, vault):
        """Point one pool member at a known-bad slug; turn marks it unavailable,
        cascades to the next live model, model_health.json shows it dead."""

    def test_error_classification_accurate(self, server, vault):
        """Force a 404/provider-error; HandoverEvent.reason == 'model_unavailable',
        NOT 'rate_limited'. (Regression for the 2026-05-29 bug.)"""

    def test_backoff_retry(self, vault):
        """Mark a model unavailable, advance past backoff, confirm it becomes
        eligible again on the next selection."""

    def test_quota_shared_cooldown(self, server, vault):
        """Simulate a 429+quota from one pool member; all OpenRouter members are
        cooled via openrouter_shared; cascade reaches the Ollama floor."""

    def test_pin_overrides_health(self, server, vault):
        """Pin a dead model; selection honors the pin; on failure it falls through."""

    def test_classification_logged(self, server, vault):
        """Coding and casual turns each produce a turn_classified event with a
        sensible class; route_by_role off → selection unchanged."""

    def test_legacy_schema2_still_works(self, vault):
        """A schema-2 fixed-rung config still loads and cascades (backward compat)."""
```

## Manual acceptance report (`docs/phase-2c-acceptance-report.md`)

```markdown
# Phase 2c Acceptance Report

Date: ____   Environment: macOS ____ | Python ____ | Plugin: P2c-T06
Orchestrator path: ~/Documents/nexus/nexus   Vault: ~/nexus-test-vault

## Eight criteria
- [ ] 1. pytest tests/ passes — `__ passed, __ skipped`.
- [ ] 2. 404/provider-error → model-unavailable, skipped, accurate user message. *JSONL + handover line screenshot.*
- [ ] 3. 429+quota → shared cooldown; 429 plain → short per-model cooldown. *Log excerpt.*
- [ ] 4. Pool loads; strongest live model selected by tier. *JSONL model_id.*
- [ ] 5. Dead model retried after backoff. *model_health.json before/after.*
- [ ] 6. UI shows who answered + skipped reasons. *Screenshot.*
- [ ] 7. turn_classified logged; role routing works when flag on. *JSONL.*
- [ ] 8. Live: cascade routes to a working frontier model with no manual slug edits; forced failure detected. *Evidence.*

## Real-availability snapshot at acceptance
(Re-run the per-model probe loop; record which pool members are live today.)

## Drift discovered
(new OPEN_QUESTIONS.md entries)

## Sign-off
Phase 2c ready for next phase: ☐ Yes / ☐ No — blockers: __
```

## Update OPEN_QUESTIONS.md

Add entries for:
- The 2026-05-29 incident + root cause (dead provider misclassified as rate-limit) and how T01 fixed it.
- The two-copy split-brain that was consolidated (code → `~/Documents/nexus`, vault → `~/nexus-test-vault`, LaunchAgent repointed). Note this so it's never re-introduced.
- `route_by_role` ships off; flip-on criteria = classifier accuracy review after soak.
- Provider-diversity note: pool is OpenRouter-only; Groq/Cerebras/FreeLLMAPI remain a future capacity option.

## Definition of done

- [ ] Integration tests written and passing (--real-providers gated).
- [ ] All eight criteria green in the report.
- [ ] OPEN_QUESTIONS.md updated.
- [ ] One commit: `feat(phase-2c): acceptance`.
- [ ] Tag the schema-2 → schema-3 migration clearly in the commit body.
- [ ] Do not start the dashboard track until this report is committed.
