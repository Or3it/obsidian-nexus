# P1-T07 — Obsidian Plugin Chat View

## Goal

A minimal Obsidian plugin that opens a "Nexus Chat" view, accepts user input, sends it to the local orchestrator, and renders the streamed response. No model picker, no quota indicator, no cascade visualiser. Phase 1 chat surface, full stop.

## Read first

- `CLAUDE.md` (rules #2 and #6 about no telemetry and verify-at-build-time)
- P1-T06 (the HTTP/SSE contract this plugin consumes)

## Files to create or replace

```
plugin/main.ts                       ← replaces stub
plugin/views/chat-view.ts            ← replaces stub
plugin/settings-tab.ts               ← new — settings UI
plugin/lib/nexus-client.ts           ← new — wraps fetch to orchestrator
plugin/lib/sse-parser.ts             ← new — minimal SSE parser
plugin/styles.css                    ← chat UI styling
plugin/manifest.json                 ← (already exists, may need version bump)
```

## Plugin entry (`plugin/main.ts`)

```typescript
import { Plugin, WorkspaceLeaf, Notice } from "obsidian";
import { ChatView, NEXUS_CHAT_VIEW_TYPE } from "./views/chat-view";
import { NexusSettingTab } from "./settings-tab";

export interface NexusSettings {
    serverUrl: string;
    authTokenOverride: string;       // empty = auto-read from vault on every load
    cachedAuthToken: string;         // last successful read; transient
}

const DEFAULT_SETTINGS: NexusSettings = {
    serverUrl: "http://127.0.0.1:7777",
    authTokenOverride: "",
    cachedAuthToken: "",
};

export default class NexusPlugin extends Plugin {
    settings: NexusSettings = DEFAULT_SETTINGS;

    async onload() {
        await this.loadSettings();
        await this.refreshAuthToken();

        this.registerView(NEXUS_CHAT_VIEW_TYPE, (leaf) => new ChatView(leaf, this));
        this.addSettingTab(new NexusSettingTab(this.app, this));

        this.addRibbonIcon("message-square", "Open Nexus Chat", () => {
            this.activateChatView();
        });

        this.addCommand({
            id: "open-nexus-chat",
            name: "Open Nexus Chat",
            callback: () => this.activateChatView(),
        });
    }

    async loadSettings() {
        this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    }

    async saveSettings() {
        await this.saveData(this.settings);
    }

    /**
     * Returns the active auth token. Override wins if set; otherwise
     * the cached value from the last successful vault read is used.
     */
    getAuthToken(): string {
        const override = this.settings.authTokenOverride.trim();
        if (override) return override;
        return this.settings.cachedAuthToken.trim();
    }

    /**
     * Read the auth token from the vault and cache it. Called on plugin load
     * and on demand from the settings tab. Does NOT overwrite the manual override.
     */
    async refreshAuthToken(): Promise<{ found: boolean; reason?: string }> {
        const tokenPath = "Orchestrator/Config/auth.token";
        try {
            const exists = await this.app.vault.adapter.exists(tokenPath);
            if (!exists) {
                if (!this.settings.authTokenOverride) {
                    new Notice(
                        "Nexus: auth.token not found in vault. " +
                        "Start the orchestrator, or paste the token in Settings → Nexus."
                    );
                }
                return { found: false, reason: "file not found in this vault" };
            }
            const content = await this.app.vault.adapter.read(tokenPath);
            this.settings.cachedAuthToken = content.trim();
            await this.saveSettings();
            return { found: true };
        } catch (e) {
            return { found: false, reason: (e as Error).message };
        }
    }

    async activateChatView() {
        const { workspace } = this.app;
        let leaf = workspace.getLeavesOfType(NEXUS_CHAT_VIEW_TYPE)[0];
        if (!leaf) {
            leaf = workspace.getRightLeaf(false);
            await leaf.setViewState({ type: NEXUS_CHAT_VIEW_TYPE, active: true });
        }
        workspace.revealLeaf(leaf);
    }
}
```

## Settings tab (`plugin/settings-tab.ts`)

The settings tab is **the recovery path** when the auto-read fails — most commonly because the user's Obsidian vault and the path passed to `python -m orchestrator --vault ...` are different directories. Without this fallback, the failure mode is silent (auth 401s with no clear cause).

```typescript
import { App, PluginSettingTab, Setting } from "obsidian";
import type NexusPlugin from "./main";

export class NexusSettingTab extends PluginSettingTab {
    plugin: NexusPlugin;

    constructor(app: App, plugin: NexusPlugin) {
        super(app, plugin);
        this.plugin = plugin;
    }

    display(): void {
        const { containerEl } = this;
        containerEl.empty();

        containerEl.createEl("h2", { text: "Nexus" });

        new Setting(containerEl)
            .setName("Orchestrator URL")
            .setDesc("Localhost address of the orchestrator. Default: http://127.0.0.1:7777.")
            .addText((text) =>
                text
                    .setValue(this.plugin.settings.serverUrl)
                    .onChange(async (value) => {
                        this.plugin.settings.serverUrl = value.trim();
                        await this.plugin.saveSettings();
                    })
            );

        containerEl.createEl("h3", { text: "Authentication" });

        const cached = this.plugin.settings.cachedAuthToken;
        const override = this.plugin.settings.authTokenOverride;
        const activeToken = this.plugin.getAuthToken();
        const source = override
            ? "manual override"
            : cached
                ? "auto-read from this vault"
                : "(none — orchestrator probably not running)";

        new Setting(containerEl)
            .setName("Active token source")
            .setDesc(source)
            .addButton((btn) =>
                btn.setButtonText("Re-read from vault").onClick(async () => {
                    const result = await this.plugin.refreshAuthToken();
                    this.display();
                    if (!result.found) {
                        new Notice(`Nexus: ${result.reason ?? "auth.token not found"}`);
                    }
                })
            );

        new Setting(containerEl)
            .setName("Manual token override")
            .setDesc(
                "Leave empty to auto-read from this vault's Orchestrator/Config/auth.token. " +
                "Set this only if the orchestrator is pointing at a different vault directory " +
                "than this one — for example if you ran `python -m orchestrator --vault /some/other/path`. " +
                "Find the token by running: `cat /that/path/Orchestrator/Config/auth.token`."
            )
            .addText((text) =>
                text
                    .setPlaceholder("(empty = auto)")
                    .setValue(this.plugin.settings.authTokenOverride)
                    .onChange(async (value) => {
                        this.plugin.settings.authTokenOverride = value.trim();
                        await this.plugin.saveSettings();
                    })
            );

        if (activeToken) {
            const preview = activeToken.length > 12 ? `${activeToken.slice(0, 8)}…` : activeToken;
            containerEl.createEl("div", {
                text: `Currently using token: ${preview}`,
                cls: "nexus-settings-token-preview",
            });
        } else {
            containerEl.createEl("div", {
                text: "No auth token available. Sending a message will fail until this is resolved.",
                cls: "nexus-settings-token-warning",
            });
        }
    }
}
```

## Chat view (`plugin/views/chat-view.ts`)

```typescript
import { ItemView, WorkspaceLeaf } from "obsidian";
import type NexusPlugin from "../main";
import { NexusClient } from "../lib/nexus-client";

export const NEXUS_CHAT_VIEW_TYPE = "nexus-chat-view";

interface Turn {
    role: "user" | "assistant";
    content: string;
    pending?: boolean;
}

export class ChatView extends ItemView {
    plugin: NexusPlugin;
    client: NexusClient;
    threadId: string | null = null;
    turns: Turn[] = [];
    transcriptEl!: HTMLElement;
    inputEl!: HTMLTextAreaElement;
    sendBtn!: HTMLButtonElement;

    constructor(leaf: WorkspaceLeaf, plugin: NexusPlugin) {
        super(leaf);
        this.plugin = plugin;
        // Token getter reads from plugin every call, so settings changes take effect immediately.
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
        header.createEl("div", { text: "Sonnet 4.6", cls: "nexus-chat-model-pill" });

        this.transcriptEl = root.createDiv({ cls: "nexus-chat-transcript" });

        const inputBar = root.createDiv({ cls: "nexus-chat-input-bar" });
        this.inputEl = inputBar.createEl("textarea", {
            cls: "nexus-chat-input",
            attr: { placeholder: "Message the Nexus...", rows: "2" },
        });
        this.sendBtn = inputBar.createEl("button", { text: "Send", cls: "nexus-chat-send" });
        this.sendBtn.onclick = () => this.sendTurn();
        this.inputEl.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                this.sendTurn();
            }
        });

        await this.renderTranscript();
    }

    async sendTurn() {
        const msg = this.inputEl.value.trim();
        if (!msg) return;
        this.inputEl.value = "";
        this.inputEl.disabled = true;
        this.sendBtn.disabled = true;

        this.turns.push({ role: "user", content: msg });
        const assistantTurn: Turn = { role: "assistant", content: "", pending: true };
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
                } else if (evt.event === "done") {
                    assistantTurn.pending = false;
                }
            }
        } catch (e) {
            assistantTurn.content = `*Error: ${(e as Error).message}*`;
            assistantTurn.pending = false;
            await this.renderTranscript();
        } finally {
            this.inputEl.disabled = false;
            this.sendBtn.disabled = false;
            this.inputEl.focus();
        }
    }

    async renderTranscript() {
        this.transcriptEl.empty();
        for (const turn of this.turns) {
            const block = this.transcriptEl.createDiv({
                cls: `nexus-chat-turn nexus-chat-turn-${turn.role}` + (turn.pending ? " is-pending" : ""),
            });
            block.createDiv({ cls: "nexus-chat-turn-role", text: turn.role === "user" ? "You" : "Sonnet 4.6" });
            block.createDiv({ cls: "nexus-chat-turn-content", text: turn.content || (turn.pending ? "…" : "") });
        }
        this.transcriptEl.scrollTop = this.transcriptEl.scrollHeight;
    }

    async onClose() {
        // No-op for Phase 1.
    }
}
```

## SSE-aware client (`plugin/lib/nexus-client.ts`)

Use `fetch` with manual response-body reading; Obsidian's renderer supports `ReadableStream`. Implement the SSE parsing in `lib/sse-parser.ts` (minimal — handle `event:` and `data:` lines, blank line as delimiter).

```typescript
import { parseSSEEvents } from "./sse-parser";

export interface StreamEvent {
    event: string;
    data: string;
}

export class NexusClient {
    constructor(
        private getBaseUrl: () => string,
        private getToken: () => string,
    ) {}

    async *streamTurn(threadId: string | null, userMessage: string): AsyncIterable<StreamEvent> {
        const token = this.getToken();
        if (!token) {
            throw new Error(
                "No auth token. Start the orchestrator or paste a token override in " +
                "Settings → Nexus."
            );
        }
        const res = await fetch(`${this.getBaseUrl()}/turn`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${token}`,
            },
            body: JSON.stringify({ thread_id: threadId, user_message: userMessage }),
        });
        if (!res.ok) {
            if (res.status === 401) {
                throw new Error(
                    "Auth rejected (401). The token in Obsidian doesn't match the orchestrator's " +
                    "token. Re-read it from the vault (Settings → Nexus → Re-read), or paste it manually."
                );
            }
            throw new Error(`Nexus server returned ${res.status}`);
        }
        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const events = parseSSEEvents(buffer);
            buffer = events.remainder;
            for (const evt of events.events) yield evt;
        }
    }

    async listThreads(): Promise<unknown[]> {
        const token = this.getToken();
        if (!token) throw new Error("No auth token.");
        const res = await fetch(`${this.getBaseUrl()}/threads`, {
            headers: { "Authorization": `Bearer ${token}` },
        });
        if (!res.ok) throw new Error(`List threads failed: ${res.status}`);
        return await res.json();
    }
}
```

`sse-parser.ts`:

```typescript
import { StreamEvent } from "./nexus-client";

export function parseSSEEvents(buffer: string): { events: StreamEvent[]; remainder: string } {
    const events: StreamEvent[] = [];
    const parts = buffer.split("\n\n");
    const remainder = parts.pop() ?? "";    // last (possibly incomplete) chunk
    for (const part of parts) {
        let event = "message";
        let data = "";
        for (const line of part.split("\n")) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trim();
        }
        if (data) events.push({ event, data });
    }
    return { events, remainder };
}
```

## Styles (`plugin/styles.css`)

Minimal, Obsidian-theme-respecting. Use the theme's CSS variables so dark/light themes work:

```css
.nexus-chat-root { display: flex; flex-direction: column; height: 100%; }
.nexus-chat-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 12px; border-bottom: 1px solid var(--background-modifier-border);
}
.nexus-chat-title { font-weight: 600; }
.nexus-chat-model-pill {
    padding: 2px 8px; border-radius: 12px;
    background: var(--interactive-accent); color: var(--text-on-accent);
    font-size: 0.75em;
}
.nexus-chat-transcript {
    flex: 1; overflow-y: auto; padding: 12px;
}
.nexus-chat-turn { margin-bottom: 16px; }
.nexus-chat-turn-role {
    font-size: 0.75em; color: var(--text-muted); margin-bottom: 4px;
}
.nexus-chat-turn-content {
    white-space: pre-wrap; line-height: 1.5;
}
.nexus-chat-turn.is-pending .nexus-chat-turn-content {
    opacity: 0.7;
}
.nexus-chat-input-bar {
    padding: 8px 12px; border-top: 1px solid var(--background-modifier-border);
    display: flex; gap: 8px;
}
.nexus-chat-input {
    flex: 1; resize: vertical;
    background: var(--background-primary); color: var(--text-normal);
    border: 1px solid var(--background-modifier-border); border-radius: 4px;
    padding: 6px 8px;
}
.nexus-chat-send {
    padding: 0 16px;
    background: var(--interactive-accent); color: var(--text-on-accent);
    border: none; border-radius: 4px; cursor: pointer;
}
.nexus-chat-send:disabled { opacity: 0.5; cursor: not-allowed; }
.nexus-settings-token-preview {
    font-family: var(--font-monospace); font-size: 0.85em;
    color: var(--text-muted); margin-top: 8px;
}
.nexus-settings-token-warning {
    color: var(--text-error); margin-top: 8px;
}
```

## Behaviour notes

1. **Auth token resolution.** Manual override wins if set; otherwise the cached value from the last successful vault read is used. Auto-read happens at plugin load. The user can force a re-read from the settings tab.
2. **The mismatched-vault case is handled.** If the user runs `python -m orchestrator --vault /a/path` but Obsidian has `/another/path` open, the auto-read finds nothing. The settings tab tells them exactly what to do: paste the token from the orchestrator's vault. This is the failure mode I expect 80% of first-time setups to hit.
3. **The plugin does not start the orchestrator.** Phase 1 requires the user to run `python -m orchestrator --vault /path/to/vault` separately. The onboarding wizard (Phase 5) wires this together.
4. **No persistent thread state in the plugin.** Refreshing the view starts at "new thread." The orchestrator owns thread history; Phase 4 ships a thread browser.
5. **No streaming-cancel UI.** Send is greyed out while a response is streaming. Fine for Phase 1.
6. **Errors render inline.** Auth and adapter errors come back via the SSE error event or fetch rejection; the plugin renders the message in the assistant turn block.

## Definition of done

- [ ] `cd plugin && npm install && npm run build` produces `plugin/main.js`.
- [ ] Copying `plugin/` to a test vault's `.obsidian/plugins/obsidian-nexus/` (with the manifest, main.js, styles.css) and enabling the plugin produces a "Nexus Chat" view in the right sidebar.
- [ ] With the orchestrator running, sending a message streams a real Sonnet response into the chat view, with the response also persisting to `Orchestrator/Live/`.
- [ ] Reading `Orchestrator/Config/auth.token` succeeds automatically; the plugin does not prompt for the token in the happy path.
- [ ] **Settings tab works.** Opening Settings → Nexus shows the orchestrator URL, the token source, and a manual override field. Setting the override and re-trying the chat uses the override token (no Obsidian restart needed).
- [ ] **Mismatched-vault recovery works.** If the orchestrator is started with `--vault /tmp/other-vault`, the plugin's auto-read fails, the settings tab surfaces this clearly, and pasting the token from `/tmp/other-vault/Orchestrator/Config/auth.token` into the override field makes chat succeed.
- [ ] **Helpful 401 error.** Sending a turn with the wrong token returns an inline error message that mentions Settings → Nexus, not just "401".
- [ ] Theme variables work — switch Obsidian dark/light and the chat view follows.
- [ ] `tsc --noEmit` clean (strict mode, no `any` warnings).

## Definition of NOT done

- No model picker dropdown (Phase 4).
- No per-turn Opus button (Phase 4).
- No thread browser (Phase 4).
- No cost / quota indicator (Phase 4).
- No cascade visualiser (Phase 4).
- No multi-thread management within the view (Phase 4).
- No process-management — the plugin does not start, stop, or watchdog the orchestrator (Phase 5).
