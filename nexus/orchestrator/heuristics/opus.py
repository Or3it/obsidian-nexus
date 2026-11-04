"""Opus auto-escalation heuristic.

Fires when the last user message is long enough AND contains at least one
complexity-signal keyword. Both thresholds are configurable via cascade.yaml
(opus_heuristic block on Rung 1). The heuristic is disabled by default so
users on constrained Pro quotas are never surprised.
"""

from ..types import OpusHeuristicConfig

_DEFAULT_KEYWORDS: tuple[str, ...] = (
    "design",
    "tradeoff",
    "trade-off",
    "architecture",
    "review",
    "analyze",
    "analyse",
    "compare",
    "implement",
    "refactor",
    "strategy",
    "evaluate",
    "critique",
)


class OpusHeuristic:
    """Stateless text classifier: returns True when a message warrants Opus."""

    def __init__(self, config: OpusHeuristicConfig) -> None:
        self._enabled = config.enabled
        self._min_chars = config.min_chars
        kw_list = list(_DEFAULT_KEYWORDS) if config.keywords is None else config.keywords
        self._keywords = frozenset(k.lower() for k in kw_list)

    def should_escalate(self, message: str) -> bool:
        """Return True iff the message meets both the length and keyword conditions."""
        if not self._enabled:
            return False
        if len(message) < self._min_chars:
            return False
        msg_lower = message.lower()
        return any(kw in msg_lower for kw in self._keywords)
