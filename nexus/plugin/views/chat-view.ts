import { ItemView, Menu, Notice, setIcon, WorkspaceLeaf } from "obsidian";
import type NexusPlugin from "../main";
import { NexusClient } from "../lib/nexus-client";
import type {
    HandoverEventData,
    HealthResponse,
    PoolHealthEntry,
    TokenEventData,
    ErrorEventData,
    ToolCallEventData,
    ToolResultEventData,
} from "../lib/nexus-client";

export const NEXUS_CHAT_VIEW_TYPE = "nexus-chat-view";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ToolCallRecord {
    name: string;
    input: Record<string, unknown>;
    done: boolean;
}

interface Turn {
    role: "user" | "assistant";
    content: string;
    pending?: boolean;
    /** Short label shown in the role row, e.g. "Sonnet 4.6" or "MiniMax M2.5". */
    modelTag?: string;
    /** Rendered handover lines, e.g. ["⤵ nemotron skipped (unavailable) → gpt-oss"]. */
    handovers?: string[];
    /** Tool calls made during this turn (SDK web-tools path). */
    toolCalls?: ToolCallRecord[];
}

interface ActiveModel {
    rung: number;
    model: string;
}

// ---------------------------------------------------------------------------
// Rung display config — matches the FINAL locked Phase 2a cascade.
//   R1: claude-sonnet-4-6        (Anthropic Pro)
//   R2: minimax/minimax-m2.5     (OpenRouter)
//   R3: openai/gpt-oss-120b      (OpenRouter)
//   R4: deepseek/deepseek-v4-flash (OpenRouter)
//   R5: gemma4:latest            (Local Ollama)
// ---------------------------------------------------------------------------

const RUNG_COLOURS: Partial<Record<number, string>> = {
    1: "var(--color-blue)",
    2: "var(--color-green)",
    3: "var(--color-cyan)",
    4: "var(--color-orange)",
    5: "var(--text-muted)",
};

const RUNG_LABELS: Partial<Record<number, string>> = {
    1: "Sonnet 4.6",
    2: "MiniMax M2.5",
    3: "GPT-OSS 120B",
    4: "DeepSeek V4 Flash",
    5: "Local",
};

const RUNG_SLUGS: Record<number, string> = {
    1: "claude-sonnet-4-6",
    2: "minimax/minimax-m2.5:free",
    3: "openai/gpt-oss-120b:free",
    4: "deepseek/deepseek-v4-flash:free",
    5: "gemma4:latest",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Strip provider prefix and free/latest suffix; prettify known Anthropic model names. */
function shortName(model: string): string {
    const PRETTY: Record<string, string> = {
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-opus-4-7": "Opus 4.7",
        "claude-haiku-4-5": "Haiku 4.5",
    };
    const base = (model.split("/").pop() ?? model)
        .replace(/:free$/, "")
        .replace(/:latest$/, "");
    if (PRETTY[base]) return PRETTY[base];
    // For long names (e.g. nemotron-3-super-120b-a12b) take first 3 dash-segments
    const parts = base.split("-");
    return parts.length > 3 ? parts.slice(0, 3).join("-") : base;
}

const REASON_LABEL: Record<string, string> = {
    model_unavailable: "unavailable",
    rate_limited:      "rate-limited",
    quota_exhausted:   "quota reached",
};

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

export class ChatView extends ItemView {
    plugin: NexusPlugin;
    client: NexusClient;
    threadId: string | null = null;
    turns: Turn[] = [];
    transcriptEl!: HTMLElement;
    inputEl!: HTMLTextAreaElement;
    sendBtn!: HTMLButtonElement;
    opusBtn!: HTMLButtonElement;
    modelPillEl!: HTMLElement;
    activeModel: ActiveModel = { rung: 1, model: "claude-sonnet-4-6" };
    pinnedRung: number | null = null;
    opusToggled = false;

    constructor(leaf: WorkspaceLeaf, plugin: NexusPlugin) {
        super(leaf);
        this.plugin = plugin;
        this.client = new NexusClient(
            () => plugin.settings.serverUrl,
            () => plugin.getAuthToken(),
        );
    }

    getViewType() { return NEXUS_CHAT_VIEW_TYPE; }
    getDisplayText() { return "Nexus Chat"; }
    getIcon() { return "message-square"; }

    async onOpen() {
        const root = this.contentEl;
        root.empty();
        root.addClass("nexus-chat-root");

        const header = root.createDiv({ cls: "nexus-chat-header" });
        header.createEl("div", { text: "Nexus Chat", cls: "nexus-chat-title" });
        this.modelPillEl = header.createEl("div", {
            cls: "nexus-chat-model-pill",
            attr: { title: "Click to pin model" },
        });
        this.modelPillEl.onclick = (e) => { void this.showModelPicker(e); };
        this.renderModelPill();

        this.transcriptEl = root.createDiv({ cls: "nexus-chat-transcript" });

        const inputBar = root.createDiv({ cls: "nexus-chat-input-bar" });
        this.inputEl = inputBar.createEl("textarea", {
            cls: "nexus-chat-input",
            attr: { placeholder: "Message the Nexus...", rows: "2" },
        });
        this.opusBtn = inputBar.createEl("button", {
            cls: "nexus-opus-btn",
            attr: { title: "Use Opus for this turn only (resets after send)" },
        });
        this.renderOpusBtn();
        this.opusBtn.onclick = () => { this.toggleOpus(); };
        this.sendBtn = inputBar.createEl("button", { text: "Send", cls: "nexus-chat-send" });
        this.sendBtn.onclick = () => { void this.sendTurn(); };
        this.inputEl.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void this.sendTurn();
            }
        });

        await this.renderTranscript();
    }

    renderModelPill() {
        const { rung, model } = this.activeModel;
        // rung=0 means a pool-path model (no fixed rung number)
        const label = rung > 0 ? (RUNG_LABELS[rung] ?? shortName(model)) : shortName(model);
        const colour = rung > 0 ? (RUNG_COLOURS[rung] ?? "var(--text-muted)") : "var(--text-muted)";
        this.modelPillEl.empty();
        this.modelPillEl.style.setProperty("--rung-colour", colour);
        if (rung > 0) {
            this.modelPillEl.createSpan({ text: `R${rung}`, cls: "nexus-chat-model-pill-rung" });
        }
        this.modelPillEl.createSpan({ text: label, cls: "nexus-chat-model-pill-label" });
        if (this.pinnedRung !== null) {
            const pinEl = this.modelPillEl.createSpan({ cls: "nexus-chat-model-pill-pin" });
            setIcon(pinEl, "pin");
            this.modelPillEl.addClass("is-pinned");
        } else {
            this.modelPillEl.removeClass("is-pinned");
        }
    }

    async showModelPicker(e: MouseEvent) {
        if (!this.threadId) {
            new Notice("Start a conversation first to pin a model.");
            return;
        }
        // Toggle: click again to dismiss existing picker
        if (document.querySelector(".nexus-pool-picker")) {
            document.querySelector(".nexus-pool-picker")?.remove();
            return;
        }

        let health: HealthResponse | undefined;
        try {
            health = await this.client.getHealth();
        } catch {
            // Ignore — fall through to legacy menu
        }

        if (health?.pool && health.pool.length > 0) {
            this.showPoolPicker(e, health.pool);
        } else {
            this.showLegacyMenu(e);
        }
    }

    private showLegacyMenu(e: MouseEvent) {
        const menu = new Menu();
        for (let r = 1; r <= 5; r++) {
            const slug = RUNG_SLUGS[r];
            const label = RUNG_LABELS[r] ?? slug;
            menu.addItem(item =>
                item
                    .setTitle(`R${r} — ${label}`)
                    .setChecked(this.pinnedRung === r)
                    .onClick(() => { void this.pinToRung(r, slug); })
            );
        }
        if (this.pinnedRung !== null) {
            menu.addSeparator();
            menu.addItem(item =>
                item.setTitle("Unpin").onClick(() => { void this.unpin(); })
            );
        }
        menu.showAtMouseEvent(e);
    }

    private showPoolPicker(e: MouseEvent, pool: PoolHealthEntry[]) {
        const panel = document.createElement("div");
        panel.className = "nexus-pool-picker nexus-glass";

        // Anchor below the pill
        const pillRect = this.modelPillEl.getBoundingClientRect();
        panel.style.cssText =
            `position:fixed;` +
            `right:${Math.max(8, window.innerWidth - pillRect.right)}px;` +
            `top:${pillRect.bottom + 4}px;` +
            `z-index:9999;`;

        // Header
        const hdr = panel.createEl("div", { cls: "nexus-picker-header", text: "Routing status" });
        hdr.style.cssText = "padding:6px 12px 4px;font-size:0.72em;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;";

        for (const entry of pool) {
            const row = panel.createEl("div", {
                cls: "nexus-picker-row" + (entry.state === "dead" ? " is-dead" : ""),
            });

            // Health dot
            const dot = row.createEl("span", { cls: "nexus-picker-health" });
            if (entry.state === "live") {
                dot.setText("●");
                dot.style.color = "var(--color-green)";
            } else if (entry.state === "dead") {
                dot.setText("●");
            } else {
                dot.setText("○");
            }

            row.createEl("span", { text: shortName(entry.model) });

            if (entry.state === "dead" && entry.until) {
                const retryTime = new Date(entry.until).toLocaleTimeString([], {
                    hour: "2-digit", minute: "2-digit",
                });
                const info = row.createEl("span", {
                    cls: "nexus-picker-dead-info",
                    text: ` retry ${retryTime}`,
                    attr: { title: entry.reason ?? "" },
                });
                info.style.cssText = "font-size:0.8em;color:var(--text-faint);";
            }

            row.addEventListener("click", () => {
                void this.pinToRung(0, entry.model);
                panel.remove();
            });
        }

        if (this.pinnedRung !== null) {
            const sep = panel.createEl("hr");
            sep.style.margin = "4px 8px";
            const unpinRow = panel.createEl("div", {
                cls: "nexus-picker-row",
                text: "Unpin",
            });
            unpinRow.addEventListener("click", () => {
                void this.unpin();
                panel.remove();
            });
        }

        document.body.appendChild(panel);

        // Close on outside click (deferred so this event doesn't immediately close it)
        setTimeout(() => {
            const close = (ev: MouseEvent) => {
                if (!panel.contains(ev.target as Node)) {
                    panel.remove();
                    document.removeEventListener("click", close);
                }
            };
            document.addEventListener("click", close);
        }, 0);
    }

    async pinToRung(rung: number, slug: string) {
        if (!this.threadId) return;
        try {
            await this.client.pinThread(this.threadId, slug);
            this.pinnedRung = rung;
            this.activeModel = { rung, model: slug };
            this.renderModelPill();
        } catch (e) {
            new Notice(`Pin failed: ${(e as Error).message}`);
        }
    }

    async unpin() {
        if (!this.threadId) return;
        try {
            await this.client.unpinThread(this.threadId);
            this.pinnedRung = null;
            this.renderModelPill();
        } catch (e) {
            new Notice(`Unpin failed: ${(e as Error).message}`);
        }
    }

    toggleOpus() {
        this.opusToggled = !this.opusToggled;
        this.renderOpusBtn();
    }

    renderOpusBtn() {
        if (this.opusToggled) {
            this.opusBtn.setText("Opus ✓");
            this.opusBtn.addClass("is-active");
        } else {
            this.opusBtn.setText("Opus");
            this.opusBtn.removeClass("is-active");
        }
    }

    async sendTurn() {
        const rawMsg = this.inputEl.value.trim();
        if (!rawMsg) return;

        // Capture toggle state before clearing it — appended to server payload only.
        const opusThisTurn = this.opusToggled;
        const msgToSend = opusThisTurn ? rawMsg + " #opus" : rawMsg;

        this.inputEl.value = "";
        this.inputEl.disabled = true;
        this.sendBtn.disabled = true;
        this.opusBtn.disabled = true;

        // Transcript shows the raw message — #opus tag is never displayed.
        this.turns.push({ role: "user", content: rawMsg });
        const assistantTurn: Turn = {
            role: "assistant",
            content: "",
            pending: true,
            modelTag: RUNG_LABELS[this.activeModel.rung] ?? this.activeModel.model,
        };
        this.turns.push(assistantTurn);
        await this.renderTranscript();

        try {
            for await (const evt of this.client.streamTurn(this.threadId, msgToSend)) {
                if (evt.event === "thread") {
                    this.threadId = (JSON.parse(evt.data) as { thread_id: string }).thread_id;
                } else if (evt.event === "token") {
                    const tok = JSON.parse(evt.data) as TokenEventData;
                    assistantTurn.content += tok.text;
                    await this.renderTranscript();
                } else if (evt.event === "handover") {
                    const ho = JSON.parse(evt.data) as HandoverEventData;
                    this.activeModel = { rung: ho.to_rung, model: ho.to_model };
                    this.renderModelPill();
                    assistantTurn.modelTag = shortName(ho.to_model);
                    const fromShort = shortName(ho.from_model);
                    const toShort = shortName(ho.to_model);
                    const reasonLabel = REASON_LABEL[ho.reason] ?? ho.reason ?? "switched";
                    if (!assistantTurn.handovers) assistantTurn.handovers = [];
                    assistantTurn.handovers.push(
                        `⤵ ${fromShort} skipped (${reasonLabel}) → ${toShort}`
                    );
                    this.showHandoverToast(ho);
                    await this.renderTranscript();
                } else if (evt.event === "tool_call") {
                    const tc = JSON.parse(evt.data) as ToolCallEventData;
                    if (!assistantTurn.toolCalls) assistantTurn.toolCalls = [];
                    assistantTurn.toolCalls.push({ name: tc.name, input: tc.input, done: false });
                    await this.renderTranscript();
                } else if (evt.event === "tool_result") {
                    const tr = JSON.parse(evt.data) as ToolResultEventData;
                    if (assistantTurn.toolCalls) {
                        for (let i = assistantTurn.toolCalls.length - 1; i >= 0; i--) {
                            const tc = assistantTurn.toolCalls[i];
                            if (tc.name === tr.name && !tc.done) {
                                tc.done = true;
                                break;
                            }
                        }
                    }
                    await this.renderTranscript();
                } else if (evt.event === "error") {
                    const err = JSON.parse(evt.data) as ErrorEventData;
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
            this.opusToggled = false;
            this.renderOpusBtn();
            await this.renderTranscript();
            this.inputEl.disabled = false;
            this.sendBtn.disabled = false;
            this.opusBtn.disabled = false;
            this.inputEl.focus();
        }
    }

    showHandoverToast(ho: HandoverEventData) {
        const toLabel = shortName(ho.to_model);
        const reason = REASON_LABEL[ho.reason] ?? this.humanReason(ho.trigger);
        new Notice(`Continuing on ${toLabel} (${reason})`, 6000);
    }

    humanReason(trigger: string): string {
        switch (trigger) {
            case "PRO_QUOTA_EXHAUSTED": return "Pro quota reached";
            case "OPENROUTER_QUOTA":    return "OpenRouter daily limit reached";
            case "HARD_FAIL":           return "previous model unavailable";
            case "SOFT_DEGRADE":        return "previous model slow";
            case "CONTEXT_LIMIT":       return "context too long for previous model";
            case "EXPLICIT_REFUSAL":    return "previous model refused";
            case "MANUAL":              return "manual switch";
            default:                    return trigger;
        }
    }

    async renderTranscript() {
        this.transcriptEl.empty();
        for (const turn of this.turns) {
            const cls =
                `nexus-chat-turn nexus-chat-turn-${turn.role}` +
                (turn.pending ? " is-pending" : "");
            const block = this.transcriptEl.createDiv({ cls });
            const roleLabel =
                turn.role === "user"
                    ? "You"
                    : (turn.modelTag ?? "Assistant");
            block.createDiv({ cls: "nexus-chat-turn-role", text: roleLabel });
            block.createDiv({
                cls: "nexus-chat-turn-content",
                text: turn.content || (turn.pending ? "…" : ""),
            });
            if (turn.toolCalls?.length) {
                const tcEl = block.createDiv({ cls: "nexus-tool-calls" });
                for (const tc of turn.toolCalls) {
                    let label: string;
                    if (tc.name === "web_search") {
                        const query = (tc.input as { query?: string }).query ?? "";
                        label = `🔍 ${query}`;
                    } else if (tc.name === "web_fetch") {
                        const url = (tc.input as { url?: string }).url ?? "";
                        try { label = `🌐 ${new URL(url).hostname}`; }
                        catch { label = `🌐 ${url}`; }
                    } else {
                        label = `⚡ ${tc.name}`;
                    }
                    tcEl.createDiv({
                        cls: "nexus-tool-call" + (tc.done ? " is-done" : " is-pending"),
                        text: label,
                    });
                }
            }
            for (const line of (turn.handovers ?? [])) {
                block.createDiv({ cls: "nexus-handover-line", text: line });
            }
        }
        this.transcriptEl.scrollTop = this.transcriptEl.scrollHeight;
    }

    async onClose() {
        // No-op for Phase 2a.
    }
}
