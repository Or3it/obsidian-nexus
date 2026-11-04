# P1-T01 — Repo Scaffolding

## Goal

Create the repository skeleton. No business logic. Just directories, manifest files, build configuration, and `.gitignore`.

## Read first

- `CLAUDE.md` (root)
- `shared/data-architecture.md`

## Files to create

```
nexus/
├── pyproject.toml
├── README.md
├── .gitignore
├── .ruff.toml                     ← or [tool.ruff] in pyproject.toml
├── orchestrator/
│   ├── __init__.py
│   ├── orchestrator.py            ← stub: just module docstring
│   ├── config.py                  ← stub
│   ├── types.py                   ← stub
│   ├── adapters/
│   │   ├── __init__.py
│   │   └── base.py                ← stub
│   └── vault/
│       ├── __init__.py
│       ├── event_log.py           ← stub
│       ├── projection.py          ← stub
│       └── frontmatter.py         ← stub
├── plugin/
│   ├── manifest.json              ← Obsidian plugin manifest
│   ├── package.json
│   ├── tsconfig.json
│   ├── esbuild.config.mjs
│   ├── main.ts                    ← stub: minimal `export default class Plugin extends Plugin {}`
│   ├── styles.css                 ← empty
│   └── views/
│       └── chat-view.ts           ← stub
├── tests/
│   ├── __init__.py
│   ├── conftest.py                ← shared fixtures
│   └── fixtures/
│       └── .gitkeep
└── OPEN_QUESTIONS.md              ← empty, with header
```

## `pyproject.toml` contents

```toml
[project]
name = "obsidian-nexus"
version = "0.1.0"
description = "Local AI orchestration layer for Obsidian — Phase 1"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.6",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "python-frontmatter>=1.0",
    "tenacity>=8.2",
    "uuid-utils>=0.7",                # for UUIDv7
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.5",
    "mypy>=1.10",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
strict = true
python_version = "3.11"
```

If `uuid-utils` doesn't exist or doesn't support UUIDv7 at build time, swap to another current library (verify via `pip search` or PyPI) and note in `OPEN_QUESTIONS.md`.

## `plugin/manifest.json`

```json
{
  "id": "obsidian-nexus",
  "name": "Obsidian Nexus",
  "version": "0.1.0",
  "minAppVersion": "1.5.0",
  "description": "Local AI orchestration layer with vault-as-memory.",
  "author": "Nate Stanley",
  "isDesktopOnly": true
}
```

## `plugin/package.json`

Scaffolded from `obsidian-sample-plugin` template. Minimum:

```json
{
  "name": "obsidian-nexus-plugin",
  "version": "0.1.0",
  "main": "main.js",
  "scripts": {
    "dev": "node esbuild.config.mjs",
    "build": "node esbuild.config.mjs production",
    "typecheck": "tsc --noEmit"
  },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "obsidian": "latest",
    "typescript": "^5.0.0",
    "esbuild": "^0.20.0"
  }
}
```

Use whatever the current `obsidian-sample-plugin` template uses for `esbuild.config.mjs` and `tsconfig.json` rather than handcrafting; this saves trouble later.

## `.gitignore`

```
# Python
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
build/
dist/
.venv/

# Node
node_modules/
main.js                            # plugin build artifact
*.log

# Editor
.vscode/
.idea/
.DS_Store
```

## `OPEN_QUESTIONS.md`

```markdown
# Open Questions

Log drift discoveries here per `shared/risks-to-verify.md`.
Each entry: ticket id, date, what was discovered, what was substituted.
```

## Definition of done

- [ ] All directories and stub files exist.
- [ ] `python -m pip install -e ".[dev]"` succeeds from a fresh venv.
- [ ] `pytest tests/` runs and reports zero tests collected without errors.
- [ ] `cd plugin && npm install && npm run build` produces `plugin/main.js` (even if it does nothing).
- [ ] `git init && git add -A && git commit -m "chore: scaffold repo"` makes a clean initial commit.

## Definition of NOT done

- Don't add business logic. Stubs only.
- Don't write tests yet (other than `conftest.py`). Tests come with the modules they test.
- Don't import anything in `orchestrator/__init__.py` beyond `__version__ = "0.1.0"`.
