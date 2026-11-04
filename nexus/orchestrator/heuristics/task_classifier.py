"""Turn task classifier: tags each user message as conversational/research/coding/general.

Two backends:
  heuristic (default) — keyword + structural rules; zero cost, instant.
  ollama (opt-in)     — local model call; zero OpenRouter quota, slightly slower.

Classification is always logged to the JSONL (turn_classified event) so the data
can be reviewed before flipping route_by_role on in cascade.yaml.
"""
from dataclasses import dataclass
from typing import Any, Literal

TaskClass = Literal["conversational", "research", "coding", "general"]

_VALID_CLASSES: frozenset[str] = frozenset(("conversational", "research", "coding", "general"))

_CODE_EXTS = (".py", ".ts", ".js", ".jsx", ".tsx", ".rs", ".go", ".cpp", ".java", ".rb", ".sh")

_RESEARCH_KEYWORDS = (
    "explain",
    "compare",
    "analyze",
    "analyse",
    "why ",
    "tradeoff",
    "trade-off",
    "history of",
    "sources",
    "evaluate",
)


@dataclass
class Classification:
    task_class: TaskClass
    confidence: Literal["high", "medium", "low"]
    reason: str


def classify_heuristic(message: str) -> Classification:
    """Classify by keyword + structural rules. Always synchronous, zero cost."""
    text = message.lower()

    # Coding: code fences, language keywords, file extensions, debug markers
    if (
        "```" in message
        or "def " in text
        or "function " in text
        or "stack trace" in text
        or "traceback" in text
        or "refactor" in text
        or "bug" in text
        or "error:" in text
        or any(ext in text for ext in _CODE_EXTS)
    ):
        return Classification("coding", "high", "code markers present")

    # Research: comparison/analysis keywords, multiple questions, long messages
    if (
        any(k in text for k in _RESEARCH_KEYWORDS)
        or message.count("?") >= 2
        or len(message) > 400
    ):
        return Classification("research", "medium", "research signals")

    # Conversational: short messages, greetings, quick follow-ups
    if len(message) < 120:
        return Classification("conversational", "medium", "short message")

    return Classification("general", "low", "no strong signal")


async def classify_ollama(message: str, adapter: Any) -> Classification:
    """Classify using a local Ollama model. Costs zero OpenRouter quota.

    ``adapter`` must implement ``complete_once(prompt, max_tokens) -> Awaitable[str]``.
    Invalid or empty output falls back to "general".
    """
    prompt = (
        "Classify this user message into exactly one word: "
        "conversational, research, coding, or general. "
        f'Message: "{message[:500]}". One word only.'
    )
    raw: str = await adapter.complete_once(prompt, max_tokens=4)
    tokens = raw.strip().lower().split()
    word = tokens[0] if tokens else ""
    task_class: TaskClass = word if word in _VALID_CLASSES else "general"  # type: ignore[assignment]
    return Classification(task_class, "medium", "ollama-classified")
