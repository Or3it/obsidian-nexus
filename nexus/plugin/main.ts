import { Notice, Plugin, WorkspaceLeaf } from "obsidian";
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
            void this.activateChatView();
        });

        this.addCommand({
            id: "open-nexus-chat",
            name: "Open Nexus Chat",
            callback: () => { void this.activateChatView(); },
        });

        this.addCommand({
            id: "open-nexus-home",
            name: "Open Nexus Home",
            callback: () => { void this.openNexusHome(); },
        });
    }

    /**
     * Open the Nexus Home web dashboard (served by the orchestrator at /app).
     * Prefers Obsidian's core Web Viewer plugin when enabled; otherwise the
     * system default browser. New dashboard features live in the web app —
     * this plugin's chat stays as-is (Phase 3W hosting decision).
     */
    async openNexusHome(): Promise<void> {
        const url = `${this.settings.serverUrl}/app`;
        // internalPlugins is outside the public API typings; runtime probe for
        // the core Web Viewer requires an untyped access.
        const internalPlugins = (this.app as unknown as {
            internalPlugins?: { getEnabledPluginById?: (id: string) => unknown };
        }).internalPlugins;
        if (internalPlugins?.getEnabledPluginById?.("webviewer")) {
            try {
                const leaf = this.app.workspace.getLeaf(true);
                await leaf.setViewState({
                    type: "webviewer",
                    state: { url, navigate: true },
                    active: true,
                });
                return;
            } catch {
                // fall through to the system browser
            }
        }
        window.open(url);
    }

    async loadSettings() {
        this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData() as Partial<NexusSettings>);
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
        let leaf: WorkspaceLeaf | undefined = workspace.getLeavesOfType(NEXUS_CHAT_VIEW_TYPE)[0];
        if (!leaf) {
            const newLeaf = workspace.getRightLeaf(false);
            if (!newLeaf) return;
            await newLeaf.setViewState({ type: NEXUS_CHAT_VIEW_TYPE, active: true });
            leaf = newLeaf;
        }
        workspace.revealLeaf(leaf);
    }
}
