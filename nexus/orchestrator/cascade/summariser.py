"""Haiku-based summariser. Used exclusively for generating handover docs.

Never appears in user-facing model selection. Routes to Haiku via
selection_hint="summariser" so the AnthropicAdapter's _resolve_model
picks the summariser_model rather than the default Sonnet path.
"""

from ..adapters.anthropic import AnthropicAdapter
from ..types import NexusMessage, NexusRequest, StyleProfile

SUMMARISER_SYSTEM_PROMPT = (
    "You are generating a handover document for a multi-model AI"
    " orchestration system. The active model is being switched."
    " Produce a Markdown document with EXACTLY these sections in order:\n"
    "\n"
    "## Summary\n"
    "## Open Questions\n"
    "## Last User Intent\n"
    "## Recommended Continuation Style\n"
    "## Full History Path\n"
    "\n"
    "Constraints:\n"
    "- Summary ≤ 500 words. Capture: what is being worked on, what has been"
    " concluded, what is contested, what the user's apparent goal is.\n"
    "- Open Questions is a bulleted list of unresolved questions in the"
    " user's words.\n"
    "- Last User Intent is ONE sentence describing what the user most"
    " recently asked for.\n"
    "- Recommended Continuation Style is one paragraph guiding the next"
    " model on tone and posture.\n"
    "- Full History Path is the wiki-link provided to you below —"
    " emit it verbatim.\n"
    "\n"
    "Output ONLY the markdown document. No preamble, no closing remarks."
)


class Summariser:
    """Wraps AnthropicAdapter with selection_hint='summariser' to route to Haiku."""

    def __init__(self, anthropic_adapter: AnthropicAdapter) -> None:
        self.adapter = anthropic_adapter

    async def summarise_for_handover(
        self,
        transcript: list[NexusMessage],
        style_profile: StyleProfile,
        full_history_wiki_link: str,
    ) -> str:
        """Generate handover body markdown. Does NOT include YAML frontmatter."""
        prompt = self._build_prompt(transcript, style_profile, full_history_wiki_link)
        request = NexusRequest(
            thread_id="summariser",
            messages=[
                NexusMessage(role="system", content=SUMMARISER_SYSTEM_PROMPT),
                NexusMessage(role="user", content=prompt),
            ],
            style_profile=StyleProfile(),
            selection_hint="summariser",
            stream=False,
        )
        response = await self.adapter.complete(request)
        return response.content

    def _build_prompt(
        self,
        transcript: list[NexusMessage],
        style_profile: StyleProfile,
        full_history_wiki_link: str,
    ) -> str:
        lines: list[str] = []
        for msg in transcript:
            if msg.role == "system":
                lines.append(f"[SYSTEM]: {msg.content}")
            elif msg.role == "user":
                lines.append(f"Human: {msg.content}")
            else:
                lines.append(f"Assistant: {msg.content}")

        transcript_text = "\n\n".join(lines)
        header = (
            f"Style profile: format={style_profile.format}, "
            f"verbosity={style_profile.verbosity}, "
            f"posture={style_profile.posture}\n"
            f"Full history wiki-link: {full_history_wiki_link}"
        )
        return f"{header}\n\nConversation transcript:\n\n{transcript_text}"
