# P2a-T10 — Plugin UI: Active-Rung Badge + Failover Toast

## Goal

The Obsidian plugin currently shows a fixed "Sonnet 4.6" pill. When the cascade fails over, the user has no feedback — the response just continues from a different model with no signal. This ticket adds:

1. An active-rung badge that updates in real time as the cascade selects a rung.
2. A non-modal toast when a handover happens.
3. Inline rendering of terminal errors in the assistant turn.

## Read first

- `nexus/plugin/views/chat-view.ts` (Phase 1 chat view)
- `nexus/plugin/lib/nexus-client.ts` (SSE parser)
- The Phase 1 styles.css

## Files to modify

```
nexus/plugin/views/chat-view.ts
nexus/plugin/lib/nexus-client.ts
nexus/plugin/styles.css
```

No new files.

## Changes to `nexus-client.ts`

The SSE parser already yields `StreamEvent`s. Phase 1 only emitted `thread`, `token`, `done`. Phase 2a adds `handover` and `error`. Extend the type:

```typescript
export interface StreamEvent {
    event: string;
    data: string;
}

export interface TokenEventData {
    type: "token";
    text: string;
    is_final: boolean;
}

export interface HandoverEventData {
    type: "handover";
    from_rung: number;
    to_rung: number;
    from_model: string;
    to_model: string;
    trigger: string;
}

export interface ErrorEventData {
    type: "error";
    message: string;
    terminal: boolean;
}
```

No actual logic change in the client — it already yields raw events.

## Changes to `chat-view.ts`

Add a small state object tracking the active rung/model. Render a pill that reflects it. Show a toast on handover. Render inline errors.

```typescript
interface Turn {
    role: "user" | "assistant";
    content: string;
    pending?: boolean;
    modelTag?: string;
    handoverFromHere?: { from: string; to: string; trigger: string };
}

interface ActiveModel {
    rung: number;
    model: string;
}

const RUNG_COLOURS: Record<number, string> = {
    1: "var(--color-blue)",
    2: "var(--color-green)",
    3: "var(--color-cyan)",
    4: "var(--color-orange)",
    5: "var(--text-muted)",
};

const RUNG_LABELS: Record<number, string> = {
    1: "Sonnet 4.6",
    2: "Gemini 3.1",
    3: "GLM 5.1",
    4: "Qwen/DeepSeek",
    5: "Local",
};


export class ChatView extends ItemView {
    // ... existing fields
    activeModel: ActiveModel = { rung: 1, model: "claude-sonnet-4-6" };
    modelPillEl!: HTMLElement;

    async onOpen() {
        // ... existing setup
        const header = root.createDiv({ cls: "nexus-chat-header" });
        header.createEl("div", { text: "Nexus Chat", cls: "nexus-chat-title" });
        this.modelPillEl = header.createEl("div", { cls: "nexus-chat-model-pill" });
        this.renderModelPill();
        // ... rest as before
    }

    renderModelPill() {
        const { rung, model } = this.activeModel;
        const label = RUNG_LABELS[rung] ?? model;
        const colour = RUNG_COLOURS[rung] ?? "var(--text-muted)";
        this.modelPillEl.empty();
        this.modelPillEl.style.setProperty("--rung-colour", colour);
        this.modelPillEl.createSpan({ text: `R${rung}`, cls: "nexus-chat-model-pill-rung" });
        this.modelPillEl.createSpan({ text: label, cls: "nexus-chat-model-pill-label" });
    }

    async sendTurn() {
        const msg = this.inputEl.value.trim();
        if (!msg) return;
        this.inputEl.value = "";
        this.inputEl.disabled = true;
        this.sendBtn.disabled = true;

        this.turns.push({ role: "user", content: msg });
        const assistantTurn: Turn = {
            role: "assistant",
            content: "",
            pending: true,
            modelTag: RUNG_LABELS[this.activeModel.rung] ?? this.activeModel.model,
        };
        this.turns.push(assistantTurn);
        await this.renderTranscript();

        try {
            for await (const evt of this.client.streamTurn(this.threadId, msg)) {
                if (evt.event === "thread") {
                    this.threadId = JSON.parse(evt.data).thread_id;
                } else if (evt.event === "token") {
                    const tok = JSON.parse(evt.data);
                    assistantTurn.content += tok.text;
                    await this.renderTranscript();
                } else if (evt.event === "handover") {
                    const ho = JSON.parse(evt.data);
                    this.activeModel = { rung: ho.to_rung, model: ho.to_model };
                    this.renderModelPill();
                    assistantTurn.modelTag = RUNG_LABELS[ho.to_rung] ?? ho.to_model;
                    assistantTurn.handoverFromHere = {
                        from: ho.from_model,
                        to: ho.to_model,
                        trigger: ho.trigger,
                    };
                    this.showHandoverToast(ho);
                    await this.renderTranscript();
                } else if (evt.event === "error") {
                    const err = JSON.parse(evt.data);
                    assistantTurn.content += `\n\n*Error: ${err.message}*`;
                    if (err.terminal) {
                        assistantTurn.pending = false;
                        await this.renderTranscript();
                        break;
                    }
                } else if (evt.event === "done") {
                    assistantTurn.pending = false;
                }
            }
        } catch (e) {
            assistantTurn.content += `\n\n*Connection error: ${(e as Error).message}*`;
            assistantTurn.pending = false;
        } finally {
            assistantTurn.pending = false;
            await this.renderTranscript();
            this.inputEl.disabled = false;
            this.sendBtn.disabled = false;
            this.inputEl.focus();
        }
    }

    showHandoverToast(ho: HandoverEventData) {
        const reason = this.humanReason(ho.trigger);
        new Notice(
            `Continuing on ${RUNG_LABELS[ho.to_rung] ?? ho.to_model} (${reason})`,
            6000,
        );
    }

    humanReason(trigger: string): string {
        switch (trigger) {
            case "PRO_QUOTA_EXHAUSTED": return "Pro quota reached";
            case "OPENROUTER_QUOTA": return "OpenRouter daily limit reached";
            case "HARD_FAIL": return "previous model unavailable";
            case "SOFT_DEGRADE": return "previous model slow";
            case "CONTEXT_LIMIT": return "context too long for previous model";
            case "EXPLICIT_REFUSAL": return "previous model refused";
            case "MANUAL": return "manual switch";
            default: return trigger;
        }
    }

    async renderTranscript() {
        this.transcriptEl.empty();
        for (const turn of this.turns) {
            const block = this.transcriptEl.createDiv({
                cls: `nexus-chat-turn nexus-chat-turn-${turn.role}` + (turn.pending ? " is-pending" : ""),
            });
            const roleLabel = turn.role === "user" ? "You" : (turn.modelTag ?? "Assistant");
            block.createDiv({ cls: "nexus-chat-turn-role", text: roleLabel });
            block.createDiv({ cls: "nexus-chat-turn-content", text: turn.content || (turn.pending ? "…" : "") });
            if (turn.handoverFromHere) {
                const divider = block.createDiv({ cls: "nexus-chat-handover-divider" });
                divider.createSpan({ text: `↓ Handover: ${turn.handoverFromHere.from} → ${turn.handoverFromHere.to} (${turn.handoverFromHere.trigger})` });
            }
        }
        this.transcriptEl.scrollTop = this.transcriptEl.scrollHeight;
    }
}
```

## Changes to `styles.css`

Add styles for the multi-part model pill, the rung indicator, the handover divider:

```css
.nexus-chat-model-pill {
    display: flex; align-items: center; gap: 6px;
    padding: 2px 8px; border-radius: 12px;
    background: var(--rung-colour, var(--interactive-accent));
    color: var(--text-on-accent);
    font-size: 0.75em;
}
.nexus-chat-model-pill-rung {
    font-weight: 700; opacity: 0.85;
}
.nexus-chat-model-pill-label {
    font-weight: 500;
}
.nexus-chat-handover-divider {
    margin-top: 8px; padding-top: 8px;
    border-top: 1px dashed var(--background-modifier-border);
    font-size: 0.7em; color: var(--text-muted);
    font-style: italic;
}
```

## Critical implementation notes

1. **The model pill is the visible *truth* of which model is answering.** It updates the instant a `handover` event arrives, before the next token streams in. The user sees the switch.

2. **The toast disappears after 6 seconds.** Long enough to read, short enough not to nag. Obsidian's `new Notice(msg, ms)` handles this.

3. **Handover marker stays in the transcript permanently.** Even after the toast vanishes, the dashed divider in the assistant turn records the handover. This matches the markdown file's representation.

4. **Error rendering doesn't blank the turn.** If the assistant has already streamed 200 tokens and then errors, those 200 tokens stay visible with the error appended. Don't replace the content.

5. **Default active model on view open is Rung 1 / Sonnet.** Even if the orchestrator is on a different rung due to a cooldown, the plugin shows what the orchestrator reports on the first `handover` event of the first turn. (Phase 4 will surface cooldown state via a separate endpoint.)

## Tests

Plugin tests are manual. There's no test harness for Obsidian plugin UI in this project. Validation is by demo:

- Run the orchestrator with a config that forces Rung 1 failure (e.g., `model: "claude-sonnet-FAKE"`).
- Send a turn from the chat view.
- Verify: pill changes from R1/Sonnet 4.6 (blue) to R2/Gemini (green); toast appears with "Continuing on Gemini 3.1"; handover divider appears in the assistant turn block; the response is real text from Gemini.

## Definition of done

- [ ] chat-view.ts updated; pill renders correctly.
- [ ] Handover toast appears on failover.
- [ ] Handover divider renders in the affected assistant turn.
- [ ] Terminal errors render inline without breaking the view.
- [ ] `tsc --noEmit` clean (strict mode, no `any` without justification).
- [ ] `npm run build` produces a clean `main.js`.
- [ ] Manual demo: trigger a Rung 1 failure, observe the full failover UX flow end-to-end.

## Definition of NOT done

- Don't add the per-turn Opus button. Phase 2b.
- Don't add the cascade visualiser panel. Phase 4.
- Don't add the cost/quota indicator. Phase 4.
- Don't add a model picker dropdown. Phase 4.
- Don't show cooldown state in the UI. Phase 4.
