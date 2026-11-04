# Obsidian Nexus

A single-user, locally-run AI orchestration layer that uses an Obsidian vault as the authoritative memory layer across multiple AI providers, with failover cascade and three-tier data sorting.

## Architecture

- **Backend orchestrator:** Python 3.11+, Pydantic 2.6+
- **Frontend plugin:** TypeScript 5+, Obsidian plugin API
- **Storage:** Plain markdown + JSONL in vault

## Setup

### Orchestrator

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Plugin

```bash
cd plugin
npm install
npm run build
```

Copy `plugin/main.js`, `plugin/manifest.json`, and `plugin/styles.css` into your vault's `.obsidian/plugins/obsidian-nexus/` directory.

## Configuration

The orchestrator reads `Orchestrator/Config/` inside your vault. Key files:

- `cascade.yaml` — model routing and Ruflo integration settings
- `budgets.yaml` — quota tracking and API caps
- `style.yaml` — style-profile defaults

## Note on `Orchestrator/` folder

The `Orchestrator/` folder inside your vault is managed exclusively by the orchestrator. Do not manually edit files inside it. In a future phase, the onboarding wizard will offer to add it to `.gitignore` and configure Obsidian Sync exclusion.

## How to verify Phase 1

### Unit tests (mocked, no Claude Code required)

```bash
source .venv/bin/activate
pytest tests/ -v
# Expected: 91 passed
```

### Integration tests (require Claude Code + Pro subscription)

```bash
# Ensure Claude Code is installed and authenticated:
claude --version   # should print 2.x
claude /login      # if not already authenticated

# Run all five end-to-end acceptance tests:
pytest tests/integration/ --real-claude -v
# Expected: 5 passed in ~45s
```

### Quick smoke test (single curl round-trip)

```bash
bash scripts/smoke.sh
# Starts a temp orchestrator, sends one turn, verifies vault artefacts, cleans up.
```

### Full acceptance evidence

See `docs/phase-1-acceptance-report.md` for the filled-in acceptance checklist with frontmatter dumps, JSONL contents, and restart-survival output.
