// The view's side of the MCP Apps bridge (the io.modelcontextprotocol/ui extension, protocol
// 2026-01-26): JSON-RPC 2.0 over postMessage with the host's window, which draws the view in a
// sandboxed frame. The view asks for nothing over the network; the host hands it the tool's result.
//
// The official App class carries a protocol client and a schema library, about 540 KB built, so the
// view speaks the handful of messages it needs itself, with the names the extension's schema gives:
//
//   view -> host  ui/initialize (request), ui/notifications/initialized, ui/notifications/size-changed
//   host -> view  ui/notifications/tool-input, ui/notifications/tool-result,
//                 ui/notifications/tool-cancelled, ui/notifications/host-context-changed,
//                 ui/resource-teardown and ping (requests, answered with an empty result)

export const PROTOCOL_VERSION = '2026-01-26';

/** A block of a tool's result: the text the agent reads, among others the view ignores. */
export interface ContentBlock {
    type: string;
    text?: string;
}

/** An MCP CallToolResult, as the host passes it on. */
export interface ToolResult {
    content?: ContentBlock[];
    structuredContent?: Record<string, unknown>;
    isError?: boolean;
}

/** What the host says about where the view is drawn; the view reads the theme and the locale. */
export interface HostContext {
    theme?: 'light' | 'dark';
    locale?: string;
    [key: string]: unknown;
}

export interface Handlers {
    result(result: ToolResult): void;
    input(args: Record<string, unknown>): void;
    context(context: HostContext): void;
    cancelled(reason: string | undefined): void;
}

/** The window the host listens on: window.parent in a frame. */
export interface Host {
    postMessage(message: unknown, targetOrigin: string): void;
}

interface Message {
    jsonrpc?: unknown;
    id?: unknown;
    method?: unknown;
    params?: unknown;
    result?: unknown;
    error?: unknown;
}

const record = (value: unknown): Record<string, unknown> =>
    value !== null && typeof value === 'object' && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : {};

export class Bridge {
    private next = 1;
    private readonly pending = new Map<number, (result: Record<string, unknown>) => void>();

    constructor(
        private readonly host: Host,
        private readonly handlers: Handlers,
        private readonly version = '',
    ) {}

    /** A message event from the page's window: only the host's own messages count. */
    receive(event: { source: unknown; data: unknown }): void {
        if (event.source !== this.host) return;
        const message = record(event.data) as Message;
        if (message.jsonrpc !== '2.0') return;
        const { id, method } = message;
        if (typeof method !== 'string') {
            if (typeof id === 'number') {
                const resolve = this.pending.get(id);
                this.pending.delete(id);
                resolve?.(record(message.result));
            }
            return;
        }
        const params = record(message.params);
        if (id !== undefined && id !== null) {
            // The host may ask the view to let go, or whether it is there; anything else it cannot do.
            if (method === 'ui/resource-teardown' || method === 'ping')
                this.post({ jsonrpc: '2.0', id, result: {} });
            else this.post({ jsonrpc: '2.0', id, error: { code: -32601, message: `no method ${method}` } });
            return;
        }
        if (method === 'ui/notifications/tool-result') this.handlers.result(params as ToolResult);
        else if (method === 'ui/notifications/tool-input') this.handlers.input(record(params.arguments));
        else if (method === 'ui/notifications/host-context-changed') this.handlers.context(params);
        else if (method === 'ui/notifications/tool-cancelled')
            this.handlers.cancelled(typeof params.reason === 'string' ? params.reason : undefined);
    }

    /** Introduce the view to the host, take what it says about itself, and say the view is ready. */
    async connect(): Promise<void> {
        const answer = await this.request('ui/initialize', {
            appInfo: { name: 'konstruct', version: this.version },
            appCapabilities: {},
            protocolVersion: PROTOCOL_VERSION,
        });
        this.handlers.context(record(answer.hostContext));
        this.post({ jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} });
    }

    /** Tell the host how tall the view is, so its frame fits it without a second scroll bar. */
    size(height: number): void {
        this.post({ jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height } });
    }

    private request(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
        const id = this.next++;
        return new Promise(resolve => {
            this.pending.set(id, resolve);
            this.post({ jsonrpc: '2.0', id, method, params });
        });
    }

    private post(message: Record<string, unknown>): void {
        // The frame is sandboxed with no origin of its own to name; the host checks the source.
        this.host.postMessage(message, '*');
    }
}
