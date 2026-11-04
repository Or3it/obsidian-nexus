import { App, Notice, PluginSettingTab, Setting } from "obsidian";
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
