# Open Questions

Log drift discoveries here per `shared/risks-to-verify.md`.
Each entry: ticket id, date, what was discovered, what was substituted.

---

## [2026-05-15] P1-T01 — hatchling requires explicit package path

**What was discovered:**

`pip install -e ".[dev]"` failed with "At least one file selection option must be defined in the `tool.hatch.build.targets.wheel` table" because the `orchestrator/` package is not inside a `src/` layout and hatchling cannot infer it.

**What was substituted / decided:**

Added `[tool.hatch.build.targets.wheel] packages = ["orchestrator"]` to `pyproject.toml`. This is the standard hatchling flat-layout fix.

**Open for future review:**

If the project grows to multiple top-level packages, the `packages` list will need updating.

---

## [2026-05-15] P1-T02 — hot-reload for config deferred to Phase 4

**What was decided:**

`config.py` loads YAML once at call time. Hot-reload (watching for file changes and re-applying valid configs without restart) is required by CLAUDE.md rule #7 but explicitly deferred. A TODO comment marks the seam in `config.py`.

**Open for future review:**

Phase 4 should add an inotify/FSEvents watcher that calls `load_cascade()` on change, swaps the running config if validation passes, and logs an error + keeps the last good config if it fails. The existing function signatures are already the right shape for this.

---

## [2026-05-15] P1-T02 — types-PyYAML added to dev dependencies

**What was discovered:**

`mypy` (strict mode) requires `types-PyYAML` stubs for the `yaml` import in `config.py`. The stubs were not in the original `pyproject.toml` dev dependencies.

**What was substituted / decided:**

Added `types-PyYAML>=6.0` to `[project.optional-dependencies] dev` in `pyproject.toml`.
