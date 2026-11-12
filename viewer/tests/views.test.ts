// The MCP App views: the bridge to the host, reading a tool's result, and each view rendered on a
// result recorded from the tool on the example project (results/, written by the MCP server).

import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { Bridge, PROTOCOL_VERSION, type ToolResult } from '../src/views/bridge';
import Conform from '../src/views/Conform.svelte';
import Explain from '../src/views/Explain.svelte';
import Gate from '../src/views/Gate.svelte';
import Impact from '../src/views/Impact.svelte';
import { PAGE, readResult } from '../src/views/result';
import { ViewState } from '../src/views/state.svelte';

interface Recorded {
    tool: string;
    arguments: Record<string, unknown>;
    result: ToolResult;
}

const files = import.meta.glob<Recorded>('./results/*.json', { eager: true, import: 'default' });
/** A fresh copy of a recorded result, free to change. */
const recorded = (name: string): Recorded => structuredClone(files[`./results/${name}.json`]!);
const data = (call: Recorded): Record<string, unknown> => call.result.structuredContent!;

function html(view: typeof Impact, call: Recorded): string {
    return render(view, { props: { state: new ViewState(call.result, call.arguments) } }).body;
}

/** The text of rendered markup, with entities decoded, as a reader sees it. */
function textOf(markup: string): string {
    return markup
        .replace(/<!--[\s\S]*?-->/g, '')
        .replace(/<[^>]+>/g, ' ')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&amp;/g, '&')
        .replace(/\s+/g, ' ');
}

describe('the bridge to the host', () => {
    function connected(): { bridge: Bridge; sent: Record<string, unknown>[]; seen: string[]; host: object } {
        const sent: Record<string, unknown>[] = [];
        const seen: string[] = [];
        const host = { postMessage: (message: unknown) => sent.push(message as Record<string, unknown>) };
        const bridge = new Bridge(
            host,
            {
                result: result => seen.push(`result ${result.isError ? 'error' : 'ok'}`),
                input: args => seen.push(`input ${JSON.stringify(args)}`),
                context: context => seen.push(`context ${context.theme ?? ''}`),
                cancelled: reason => seen.push(`cancelled ${reason}`),
            },
            '0.13.22',
        );
        return { bridge, sent, seen, host };
    }

    it('introduces the view, takes the host context and says it is ready', async () => {
        const { bridge, sent, seen, host } = connected();
        const ready = bridge.connect();
        expect(sent[0]).toEqual({
            jsonrpc: '2.0',
            id: 1,
            method: 'ui/initialize',
            params: {
                appInfo: { name: 'konstruct', version: '0.13.22' },
                appCapabilities: {},
                protocolVersion: PROTOCOL_VERSION,
            },
        });
        bridge.receive({
            source: host,
            data: { jsonrpc: '2.0', id: 1, result: { hostContext: { theme: 'light' } } },
        });
        await ready;
        expect(seen).toEqual(['context light']);
        expect(sent[1]).toEqual({ jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} });
    });

    it('passes on the input, the result, changes of context and a cancelled call', () => {
        const { bridge, seen, host } = connected();
        const notify = (method: string, params: unknown): void =>
            bridge.receive({ source: host, data: { jsonrpc: '2.0', method, params } });
        notify('ui/notifications/tool-input', { arguments: { ref: 'orders' } });
        notify('ui/notifications/tool-result', { content: [], isError: true });
        notify('ui/notifications/host-context-changed', { theme: 'dark' });
        notify('ui/notifications/tool-cancelled', { reason: 'user action' });
        expect(seen).toEqual([
            'input {"ref":"orders"}',
            'result error',
            'context dark',
            'cancelled user action',
        ]);
    });

    it('hears only its host, and only JSON-RPC', () => {
        const { bridge, seen, host } = connected();
        const result = { method: 'ui/notifications/tool-result', params: {} };
        bridge.receive({ source: {}, data: { jsonrpc: '2.0', ...result } });
        bridge.receive({ source: host, data: result });
        bridge.receive({ source: host, data: 'ui/notifications/tool-result' });
        expect(seen).toEqual([]);
    });

    it('answers teardown and ping, refuses other requests, and reports its height', () => {
        const { bridge, sent, host } = connected();
        bridge.receive({
            source: host,
            data: { jsonrpc: '2.0', id: 7, method: 'ui/resource-teardown', params: {} },
        });
        bridge.receive({ source: host, data: { jsonrpc: '2.0', id: 8, method: 'ping' } });
        bridge.receive({ source: host, data: { jsonrpc: '2.0', id: 9, method: 'tools/call', params: {} } });
        bridge.size(420);
        expect(sent).toEqual([
            { jsonrpc: '2.0', id: 7, result: {} },
            { jsonrpc: '2.0', id: 8, result: {} },
            { jsonrpc: '2.0', id: 9, error: { code: -32601, message: 'no method tools/call' } },
            { jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: 420 } },
        ]);
    });
});

describe('reading a result', () => {
    it('takes the structured copy, then the text, and shows an error as it is', () => {
        expect(readResult(recorded('gate').result)).toEqual({ data: data(recorded('gate')) });
        expect(readResult({ content: [{ type: 'text', text: '{"passed": true}' }] })).toEqual({
            data: { passed: true },
        });
        const error = recorded('impact-error').result;
        expect(readResult(error)).toEqual({ error: error.content![0]!.text });
        expect(readResult({ content: [{ type: 'text', text: 'not JSON' }] })).toEqual({ error: 'not JSON' });
    });

    it('draws the same data the text result holds', () => {
        for (const name of ['impact', 'explain-composed', 'gate']) {
            const result = recorded(name).result;
            expect(JSON.parse(result.content![0]!.text!)).toEqual(result.structuredContent);
        }
    });
});

describe('the views on recorded results', () => {
    it('impact: the steps downstream with the transform at each, and every section', () => {
        const page = textOf(html(Impact, recorded('impact')));
        expect(page).toContain(
            'Changed stg_payments.amount Step 1 customers.customer_lifetime_value transformed',
        );
        expect(page).toContain(
            'Step 2 customer_ltv.lifetime_value passed through through jaffle_shop_daily.build_customer_ltv',
        );
        expect(page).toContain(
            'Exposures 1 customer_dashboard Weekly view of customers, their orders and lifetime value. models/exposures.yml',
        );
        expect(page).toContain('Customer lifetime value approved');
        expect(page).toContain('7 Columns');
        expect(page).toContain('text from the repository');
    });

    it('impact: says when nothing is downstream', () => {
        const call = recorded('impact');
        Object.assign(data(call), { columns: [], relations: [], joins_and_filters: [], tests: [] });
        Object.assign(data(call), { exposures: [], tasks: [], terms: [], entities: {}, total: 0 });
        const page = textOf(html(Impact, call));
        expect(page).toContain('Nothing downstream.');
        expect(page).not.toContain('Step 1');
    });

    it('explain: the composed formula, its own, the joins and filters, and the plain English', () => {
        const markup = html(Explain, recorded('explain-composed'));
        const page = textOf(markup);
        expect(page).toContain('is the sum of ([amount] divided by 100).');
        expect(markup).toContain('Composed to source');
        expect(markup).toContain('Its own expression');
        expect(page).toContain(
            'LEFT JOIN ON: stg_payments.order_id = "stg_orders#1/1".order_id rows are joined',
        );
        expect(page).toContain(
            'raw_payments.amount seed No expression recorded: this column is read from a source, or the SQL that builds it could not be analysed.',
        );
        expect(page).toContain('Names [amount] raw_payments.amount');
        expect(page).toContain('Owner: Priya Shah');
        const plain = textOf(html(Explain, recorded('explain-plain')));
        expect(plain).not.toContain('rows are joined');
        expect(plain).toContain('GROUP BY: "stg_orders#1/1".customer_id');
    });

    it('impact: a refuted edge does not look like a confirmed one, and descriptions are shown', () => {
        const call = recorded('impact');
        const [first, second] = data(call).columns as Record<string, unknown>[];
        Object.assign(first!, { verified: 'refuted' });
        Object.assign(second!, { verified: 'confirmed' });
        const markup = html(Impact, call);
        expect(markup).toContain('<span class="verified warn">verify: refuted</span>');
        expect(markup).toContain('<span class="verified ok">verify: confirmed</span>');
        expect(textOf(markup)).toContain(
            'orders.amount transformed verify: confirmed Total amount (AUD) of the order',
        );
    });

    it('explain: what the command prints beyond the formula', () => {
        const call = recorded('explain-composed');
        const english = data(call).english as Record<string, unknown>;
        const [term] = english.terms as Record<string, unknown>[];
        Object.assign(term!, {
            kind: 'measure',
            filters: ["status = 'completed'"],
            english: ['status is completed'],
        });
        Object.assign(english, { sentence: null, verbatim: ['DATE_TRUNC(week, x)'], complete: false });
        const [top] = data(call).steps as Record<string, unknown>[];
        Object.assign(top!, { verified: ['confirmed'] });
        const page = textOf(html(Explain, call));
        expect(page).toContain('customers.customer_lifetime_value is the sum of ([amount] divided by 100).');
        expect(page).toContain('A column on the way has no recorded expression');
        expect(page).toContain('given as SQL: DATE_TRUNC(week, x)');
        expect(page).toContain('customers.customer_lifetime_value model verify: confirmed');
        expect(page).toContain('Customer lifetime value approved measure');
        expect(page).toContain('Unit: currency (AUD)');
        expect(page).toContain("Required filter status = 'completed' status is completed");
    });

    it('explain: verify results belong to the step, not to the input in the same place', () => {
        // verify leaves some edges unmarked, so two inputs can come with one result.
        const call = recorded('explain-composed');
        const [top] = data(call).steps as Record<string, unknown>[];
        Object.assign(top!, { inputs: ['orders.amount', 'payments.amount'], verified: ['refuted'] });
        const markup = html(Explain, call);
        const page = textOf(markup);
        expect(page).toContain('customers.customer_lifetime_value model verify: refuted');
        expect(page).toContain('Reads orders.amount payments.amount');
        expect(markup).not.toContain('<code>orders.amount</code><span class="verified');
        expect(markup.match(/class="verified/g)).toHaveLength(1);
    });

    it('conform: implementations of one name in two layers are both listed', () => {
        const call = recorded('conform');
        const findings = data(call).findings as Record<string, unknown>[];
        const differs = findings.find(f => f.name === 'completed_order');
        const against = differs!.against as Record<string, unknown>[];
        against.push({ ...against[0]!, source: 'ossie metric' });
        expect(textOf(html(Conform, call))).toContain(
            'Completed order term, name Completed order ossie metric',
        );
    });

    it('gate: the verdict and each finding with its file and fix', () => {
        const page = textOf(html(Gate, recorded('gate')));
        expect(page).toContain('Gate failed 1 finding to fix before proposing the change Compared with HEAD');
        expect(page).toContain('breaking stg_payments.amount is removed but still read by customers, orders');
        expect(page).toContain('File models/staging/stg_payments.sql');
        expect(page).toContain('Fix Keep the column, or change the models that read it in the same change.');
        expect(textOf(html(Gate, recorded('gate-passed')))).toContain(
            'Gate passed Nothing the change touches breaks.',
        );
    });

    it('conform: each verdict, the two formulas, what it was compared with and the missing filter', () => {
        const page = textOf(html(Conform, recorded('conform')));
        expect(page).toContain('2 Metrics 0 conforms 1 differs 1 unbound 0 unparsed');
        expect(page).toContain(
            'completed_order differs warning differs from a definition matched through its columns or its name alone',
        );
        expect(page).toContain(
            'In the layer COUNT(orders.order_key) What the code computes COUNT(raw_orders.id)',
        );
        expect(page).toContain(
            "Missing filter orders.status = 'completed' approved definition: Completed order",
        );
        expect(page).toContain('orders.order_key key-misfiled warning');
    });

    it('an error result is shown as the error, and no result as waiting', () => {
        expect(textOf(html(Impact, recorded('impact-error')))).toContain(
            "nothing in the graph matches 'nope'",
        );
        const waiting = render(Impact, { props: { state: new ViewState() } }).body;
        expect(textOf(waiting)).toContain('Waiting for the result');
        expect(waiting).not.toContain('untrusted');
    });
});

describe('repository text in a view', () => {
    const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';

    it('is drawn as text, never as markup', () => {
        const gate = recorded('gate');
        Object.assign((data(gate).findings as Record<string, unknown>[])[0]!, {
            message: hostile,
            path: hostile,
        });
        const impact = recorded('impact');
        Object.assign(data(impact).target as Record<string, unknown>, { label: hostile });
        const explain = recorded('explain-composed');
        Object.assign(data(explain), { composed: hostile, description: hostile });
        const conform = recorded('conform');
        Object.assign((data(conform).findings as Record<string, unknown>[])[0]!, {
            witness: hostile,
            name: hostile,
        });
        for (const [view, call] of [
            [Gate, gate],
            [Impact, impact],
            [Explain, explain],
            [Conform, conform],
        ] as const) {
            const markup = html(view as typeof Impact, call);
            expect(markup).not.toMatch(/<img|<script/);
            expect(textOf(markup)).toContain(hostile);
        }
    });
});

describe('a large result', () => {
    it('is drawn a page at a time, saying how much the text result holds', () => {
        const call = recorded('impact');
        const column = (data(call).columns as Record<string, unknown>[])[0]!;
        data(call).columns = Array.from({ length: 2500 }, (_, i) => ({
            ...column,
            id: `c${i}`,
            label: `m${i}.c`,
        }));
        const markup = html(Impact, call);
        expect(markup.match(/class="item"><code class="name">m\d+\.c</g)).toHaveLength(PAGE);
        expect(textOf(markup)).toContain('Showing 100 of 2500; the text result has every one. Show 100 more');
    });
});
