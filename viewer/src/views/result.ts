// What a view draws from a tool's result. The text block is what the agent reads and stays the
// whole answer; the view draws the same data, from the structured copy MCP results carry.

import type { ToolResult } from './bridge';

export type Read = { data: Record<string, unknown> } | { error: string };

/** The data of a tool's result, or the error to show in its place. */
export function readResult(result: ToolResult): Read {
    const text = (result.content ?? [])
        .filter(block => block.type === 'text' && typeof block.text === 'string')
        .map(block => block.text)
        .join('\n');
    if (result.isError) return { error: text };
    if (result.structuredContent) return { data: result.structuredContent };
    try {
        const data: unknown = JSON.parse(text);
        if (data !== null && typeof data === 'object' && !Array.isArray(data))
            return { data: data as Record<string, unknown> };
    } catch {
        // Text that is not a JSON object is shown as it is.
    }
    return { error: text };
}

/** How many items of a long list a view draws at first, and adds with each "show more". */
export const PAGE = 100;

/** The list ``value`` holds, or an empty one: a view draws what is there and skips the rest. */
export function list<T>(value: unknown): T[] {
    return Array.isArray(value) ? (value as T[]) : [];
}

/** ``value`` when it is a non-empty string. */
export function text(value: unknown): string | undefined {
    return typeof value === 'string' && value !== '' ? value : undefined;
}

/** Items grouped by a key, in the order the keys first appear. */
export function groupBy<T, K>(items: T[], key: (item: T) => K): [K, T[]][] {
    const groups = new Map<K, T[]>();
    for (const item of items) {
        const k = key(item);
        const group = groups.get(k);
        if (group) group.push(item);
        else groups.set(k, [item]);
    }
    return [...groups];
}
