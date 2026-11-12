// A view's state: the tool's arguments and result as the host passes them on.

import type { ToolResult } from './bridge';
import { readResult, type Read } from './result';

export class ViewState {
    args = $state<Record<string, unknown>>({});
    result = $state<ToolResult | null>(null);
    cancelled = $state<string | null>(null);
    readonly read: Read | null = $derived(this.result ? readResult(this.result) : null);

    constructor(result: ToolResult | null = null, args: Record<string, unknown> = {}) {
        this.result = result;
        this.args = args;
    }
}
