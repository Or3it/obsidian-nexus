export interface StreamEvent {
    event: string;
    data: string;
}

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
