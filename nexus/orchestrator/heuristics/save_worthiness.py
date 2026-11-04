"""Save-worthiness filter: decide whether a completed turn belongs in the
persistent JSONL log, or is low-value noise (a bare "hi"/"ok"/"thanks" and
its equally-throwaway reply) not worth keeping forever.

This never affects what the user sees — the frontend gets every token
regardless. It only gates the vault-persistence step, and it's deliberately
conservative: a turn is skipped only when BOTH the user's message and the
assistant's reply look trivial. A real question always saves, even with a
short answer.
"""
from __future__ import annotations

import re

# Lightweight regex: the *entire* (trimmed, lowercased) message is one of
# these greetings/acks, optionally with light trailing punctuation (. ! ,).
# A message that merely *contains* one of these words ("ok I'll use flask
# then") does not match — only a message that IS essentially just the word.
_PURE_GREETING_RE = re.compile(
    r"^(hi|hey|hello|hiya|yo|thanks|thank you|thx|ty|ok|okay|k|kk|cool|"
    r"nice|sure|yep|yeah|yup|nope|no|alright|got it|gotcha|sounds good|"
    r"welcome|np)[.!,]*$",
    re.IGNORECASE,
)

# Below this length, with no code fence and no question mark, a user message
# is short-and-plain enough to be a candidate for skipping.
_SHORT_MESSAGE_CHARS = 15

# Below this length, an assistant reply is a candidate for skipping.
_TRIVIAL_REPLY_CHARS = 20


def _is_short_and_plain(message: str) -> bool:
    """Short, with no code fence and no question mark."""
    stripped = message.strip()
    return len(stripped) < _SHORT_MESSAGE_CHARS and "```" not in message and "?" not in message


def _is_pure_greeting(message: str) -> bool:
    """The whole (trimmed) message is just a greeting/ack — not a substring match."""
    return bool(_PURE_GREETING_RE.match(message.strip()))


def is_worth_saving(user_message: str, assistant_content: str) -> bool:
    """Return False only when BOTH the user message and the reply are trivial.

    Conservative by design (bias toward saving): a substantive user message
    (anything not short-and-plain and not a pure greeting/ack) always saves,
    regardless of how short the reply is — "why?" answered with "Because."
    still saves. Only the combination of a throwaway message AND a
    throwaway reply is filtered.
    """
    user_trivial = _is_short_and_plain(user_message) or _is_pure_greeting(user_message)
    if not user_trivial:
        return True
    assistant_trivial = len(assistant_content.strip()) < _TRIVIAL_REPLY_CHARS
    return not assistant_trivial
