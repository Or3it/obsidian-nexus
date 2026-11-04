## Summary

The conversation focuses on designing the failover cascade architecture for the Obsidian Nexus system. The user established a 5-rung cascade: Rung 1 is Anthropic (Sonnet 4.6 default, Opus 4.7 on-demand, Haiku 4.5 for summarisation); Rungs 2–4 are OpenRouter free-tier models (MiniMax M2.5, GPT-OSS 120B, DeepSeek V4 Flash); Rung 5 is local Ollama (gemma4:latest). The architecture was concluded: each rung activates when the previous rung fails or exhausts quota. Seven trigger classes were agreed: HARD_FAIL, PRO_QUOTA_EXHAUSTED, SOFT_DEGRADE, OPENROUTER_QUOTA, CONTEXT_LIMIT, EXPLICIT_REFUSAL, and MANUAL.

## Open Questions

- What specific latency thresholds define SOFT_DEGRADE (exact multiplier and window size)?
- How should cross-rung cooldowns propagate to the plugin UI so the user knows which rungs are paused?

## Last User Intent

The user asked which trigger classes should be supported in the failover cascade.

## Recommended Continuation Style

Maintain a technical peer-level discussion, providing specific implementation details and concrete architectural decisions. The user is building a production system and expects precise proposals over exploratory brainstorming. Prefer numbered lists and code examples when describing multi-step logic.

## Full History Path

[[2026-05-16_thread-a1b2c3d4_designing-cascade]]
