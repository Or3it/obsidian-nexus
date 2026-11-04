# Risks to Verify at Build Time

The PRD contains several specific factual claims about external systems that may have drifted since it was written (May 13, 2026). **Verify before encoding these as defaults.** Where verification fails or yields ambiguous results, write the alternative into `Config/cascade.yaml` as a fallback rather than hardcoding.

## Must verify

| Claim in PRD | How to verify | What to do with the result |
|---|---|---|
| Claude Code is installed via `npm install -g @anthropic-ai/claude-code` | `npm view @anthropic-ai/claude-code` | Use the package name and current latest version |
| Claude Code login command is `claude /login` | Run `claude --help` after install | Use the actual command surfaced by `--help` |
| Claude Code accepts `--model`, `--json`, `--message`, `--print` flags | Run `claude --help` after install | Use the actual flag names; update the adapter's subprocess args |
| Ruflo's npm package name and current version | `npm view ruflo` | Pin the version in `Config/ruflo.lock` at install time |
| Ruflo's CLI surface (`npx ruflo claude-code ...`) | Run `npx ruflo --help` | Use the actual subcommand and flags |
| Anthropic API model strings: `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5` | Check current model IDs in https://docs.claude.com/en/docs/about-claude/models or via the API `/v1/models` endpoint | Use the actually-valid model strings |
| Google AI Studio model string `gemini-3-1-pro` and free-tier limits | https://ai.google.dev/pricing and AI Studio docs | Update model string + record current free-tier RPM/RPD in config |
| OpenRouter model slugs `zhipu/glm-5.1:free`, `qwen/qwen3-coder-480b:free`, `deepseek/deepseek-r1:free` | https://openrouter.ai/models (filter to free) | Use currently-listed slugs; populate fallback lists from current free models |
| OpenRouter free-tier rate limits (~20 RPM, ~200 RPD shared) | https://openrouter.ai/docs/limits | Encode current numbers in `Config/cascade.yaml` |
| Anthropic policy on third-party tools and subscription auth (April 4, 2026) | Search recent Anthropic blog/policy posts | Confirm Ruflo's wrapping approach is still permitted; if not, surface a warning to the user |
| Claude Pro weekly limits exist and were updated May 6, 2026 | Anthropic announcements; https://support.claude.com | Confirm the quota-tracking behavior matches current Pro mechanics |

## Won't verify (accept PRD as written)

These are design choices, not facts about the world:

- The five-rung cascade ordering.
- The seven-class trigger taxonomy.
- The tier 1/2/3/4 context-packing budget percentages (40/10/25/25).
- Vault folder layout.
- YAML schemas.
- The decision to use UUIDv7.
- The single-writer-per-orchestrator concurrency model.
- The decision to use Ollama for local embeddings (not sentence-transformers).

## Procedure for failed verification

If a verification returns something incompatible with the PRD (e.g., Ruflo no longer exists, or the model string has changed shape):

1. **Don't silently substitute.** Stop the ticket, append to `OPEN_QUESTIONS.md` at the repo root with the discovered drift and recommended action.
2. **Use the closest current equivalent for the build.** Configure it via the YAML, not hardcoded.
3. **Continue the ticket** with the substitute. The user can review `OPEN_QUESTIONS.md` and decide.

The point: don't block on drift, but don't hide it either.
