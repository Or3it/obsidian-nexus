import { parseSSEEvents, StreamEvent } from "./sse-parser";

export type { StreamEvent };

// ---------------------------------------------------------------------------
// Typed event-data payloads (Phase 2a)
// ---------------------------------------------------------------------------

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
    reason: string;  // "model_unavailable" | "rate_limited" | "quota_exhausted"
}

export interface PoolHealthEntry {
    model: string;
    state: "live" | "dead" | "untested";
    until?: string;     // ISO datetime when dead_until expires
    reason?: string;    // last failure reason
    failures?: number;
    last_success?: string;
}

export interface HealthResponse {
    healthy: boolean;
    last_checked: string;
    rungs: Record<string, object>;
    pool?: PoolHealthEntry[];
}

export interface ErrorEventData {
    type: "error";
    message: string;
    terminal: boolean;
}

export interface ToolCallEventData {
    type: "tool_call";
    name: string;
    input: Record<string, unknown>;
}

export interface ToolResultEventData {
    type: "tool_result";
    name: string;
    content: string;
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

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
        if (!res.body) throw new Error("Response body is null");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const parsed = parseSSEEvents(buffer);
            buffer = parsed.remainder;
            for (const evt of parsed.events) yield evt;
        }
    }

    async listThreads(): Promise<unknown[]> {
        const token = this.getToken();
        if (!token) throw new Error("No auth token.");
        const res = await fetch(`${this.getBaseUrl()}/threads`, {
            headers: { "Authorization": `Bearer ${token}` },
        });
        if (!res.ok) throw new Error(`List threads failed: ${res.status}`);
        return await res.json() as unknown[];
    }

    async pinThread(threadId: string, model: string): Promise<void> {
        const token = this.getToken();
        if (!token) throw new Error("No auth token.");
        const res = await fetch(
            `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/pin`,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`,
                },
                body: JSON.stringify({ model }),
            }
        );
        if (!res.ok) throw new Error(`Pin failed: ${res.status}`);
    }

    async unpinThread(threadId: string): Promise<void> {
        const token = this.getToken();
        if (!token) throw new Error("No auth token.");
        const res = await fetch(
            `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/pin`,
            {
                method: "DELETE",
                headers: { "Authorization": `Bearer ${token}` },
            }
        );
        if (!res.ok) throw new Error(`Unpin failed: ${res.status}`);
    }

    async getHealth(): Promise<HealthResponse> {
        const res = await fetch(`${this.getBaseUrl()}/health`);
        if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
        return await res.json() as HealthResponse;
    }
}
