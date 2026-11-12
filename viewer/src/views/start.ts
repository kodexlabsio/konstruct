// Starting a view: mount it, then, inside an MCP host's frame, connect to the host and draw the
// tool's result when the host passes it on.

import { mount, type Component } from 'svelte';
import { i18n } from '../lib/i18n.svelte';
import { Bridge, type HostContext } from './bridge';
import { ViewState } from './state.svelte';
import './views.css';

function apply(context: HostContext): void {
    const root = document.documentElement;
    if (context.theme === 'light' || context.theme === 'dark') root.setAttribute('data-theme', context.theme);
    if (typeof context.locale === 'string') i18n.choose(context.locale);
    root.lang = i18n.locale;
    root.dir = i18n.dir;
}

export function start(View: Component<{ state: ViewState }>): void {
    const state = new ViewState();
    const light = window.matchMedia?.('(prefers-color-scheme: light)').matches;
    apply({ theme: light ? 'light' : 'dark' });
    mount(View, { target: document.getElementById('root') ?? document.body, props: { state } });
    // Opened on its own, outside a host, the view has no result to draw and says it is waiting.
    if (window.parent === window) return;
    // The MCP server fills in Konstruct's version when it serves the page.
    const version = document.querySelector('meta[name="konstruct-version"]')?.getAttribute('content') ?? '';
    const bridge = new Bridge(
        window.parent,
        {
            result: result => (state.result = result),
            input: args => (state.args = args),
            context: apply,
            cancelled: reason => (state.cancelled = reason ?? ''),
        },
        version,
    );
    window.addEventListener('message', event => bridge.receive(event));
    // The host hears the view's height only once it has answered the view's introduction.
    void bridge.connect().then(() => {
        let height = 0;
        new ResizeObserver(() => {
            const now = Math.ceil(document.body.getBoundingClientRect().height);
            if (now !== height) bridge.size((height = now));
        }).observe(document.body);
    });
}
