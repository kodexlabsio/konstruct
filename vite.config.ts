import { svelte } from '@sveltejs/vite-plugin-svelte';
import type { Plugin } from 'vite';
import { viteSingleFile } from 'vite-plugin-singlefile';
import { defineConfig } from 'vitest/config';
import { minifyWgsl } from './viewer/src/gpu/wgsl.ts';
import { CATALOGUES } from './viewer/src/locales/index.ts';

/** The messages of the code block's copy button, which the views share with graph.html. */
const CODE_MESSAGES = new Set(['code.copy', 'code.copied', 'code.copyCommand', 'code.selected']);

/** The MCP App views, each built into konstruct/outputs/views/<name>.html by `vite build --mode <name>`. */
export const VIEWS = ['impact', 'explain', 'gate', 'conform'] as const;

/**
 * A Content-Security-Policy for the page, allowing its one inline script and one inline style
 * by their hashes: no 'unsafe-inline', no eval, nothing from the network but ``connect``: the
 * serving Konstruct's own address for graph.html, nothing at all for a view. The JSON the build
 * fills in is data, not script, so filling it in does not change the hashes.
 */
function contentSecurityPolicy(connect: string): Plugin {
    // Web Crypto, which Node also has, so the config needs no Node type definitions.
    const hash = async (text: string): Promise<string> => {
        const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
        let binary = '';
        for (const byte of digest) binary += String.fromCharCode(byte);
        return `'sha256-${btoa(binary)}'`;
    };
    return {
        name: 'konstruct-csp',
        enforce: 'post',
        generateBundle: {
            order: 'post',
            async handler(_, bundle) {
                for (const file of Object.values(bundle)) {
                    if (file.type !== 'asset' || !file.fileName.endsWith('.html')) continue;
                    const html = String(file.source);
                    const modules = [...html.matchAll(/<script type="module"[^>]*>([\s\S]*?)<\/script>/g)];
                    // Style elements outside the script: the script itself writes <style> into exports.
                    const rest = modules.reduce((text, m) => text.replace(m[0], ''), html);
                    const scripts = await Promise.all(modules.map(m => hash(m[1]!)));
                    const styles = await Promise.all(
                        [...rest.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => hash(m[1]!)),
                    );
                    const policy = [
                        "default-src 'none'",
                        `script-src ${scripts.join(' ')}`,
                        `style-src ${styles.join(' ')}`,
                        'font-src data:',
                        'img-src data:',
                        `connect-src ${connect}`,
                        "base-uri 'none'",
                        "form-action 'none'",
                        "object-src 'none'",
                    ].join('; ');
                    file.source = html.replace(
                        '<meta charset="utf-8" />',
                        `<meta charset="utf-8" />\n        <meta http-equiv="Content-Security-Policy" content="${policy}" />`,
                    );
                }
            },
        },
    };
}

/**
 * The message catalogues a page carries: graph.html every message but the views', a view only its
 * own and the copy button's. Each catalogue stays one file per locale; a page leaves out what it
 * cannot show, which in a view is most of the viewer's thousand messages.
 */
function catalogues(keep: (key: string) => boolean): Plugin {
    return {
        name: 'konstruct-catalogues',
        load(id) {
            if (!id.endsWith('/viewer/src/locales/index.ts')) return null;
            const kept = Object.fromEntries(
                Object.entries(CATALOGUES).map(([locale, messages]) => [
                    locale,
                    Object.fromEntries(Object.entries(messages).filter(([key]) => keep(key))),
                ]),
            );
            return { code: `export const CATALOGUES = ${JSON.stringify(kept)};`, map: null };
        },
    };
}

/** WGSL shaders imported as their source text, without comments or spare whitespace. */
function wgsl(): Plugin {
    return {
        name: 'konstruct-wgsl',
        transform(code, id) {
            if (!id.endsWith('.wgsl')) return null;
            return { code: `export default ${JSON.stringify(minifyWgsl(code))};`, map: null };
        },
    };
}

// One self-contained HTML file: script, styles and fonts inlined, nothing loaded from the network.
// It is written into the Python package, where konstruct fills the data placeholders. The toolchain
// is configured at the repository root, beside Cargo.toml and pyproject.toml; the page's own root,
// from which its entry and tests are found, is viewer/. `--mode <view>` builds one MCP App view
// instead (a single file cannot share its code with another, so each view is its own build).
export default defineConfig(({ mode }) => {
    const view = VIEWS.find(name => name === mode);
    return {
        root: 'viewer',
        // Vite's cache would otherwise be viewer/node_modules/.vite, beside nothing else of Node's.
        cacheDir: '../node_modules/.vite',
        plugins: [
            wgsl(),
            // Relative to the root above: svelte.config.js is at the repository root, for svelte-check.
            svelte({ configFile: '../svelte.config.js' }),
            viteSingleFile({ removeViteModuleLoader: true }),
            // A view is drawn in an MCP host's frame and gets its data from the host: it connects nowhere.
            contentSecurityPolicy(view ? "'none'" : "'self'"),
            ...(mode === 'test'
                ? []
                : [
                      catalogues(
                          view
                              ? key => key.startsWith('view.') || CODE_MESSAGES.has(key)
                              : key => !key.startsWith('view.'),
                      ),
                  ]),
        ],
        build: {
            // Writes konstruct/outputs/viewer.html, and a view konstruct/outputs/views/<view>.html: the
            // entry's path, in the package directory.
            outDir: '../konstruct/outputs',
            rollupOptions: { input: view ? `views/${view}.html` : 'viewer.html' },
            emptyOutDir: false,
            assetsInlineLimit: Number.MAX_SAFE_INTEGER,
            cssCodeSplit: false,
            target: 'es2022',
            reportCompressedSize: false,
            // Every page is one file with nothing to preload, so the preload polyfill is dead code.
            modulePreload: { polyfill: false },
        },
        test: {
            environment: 'node',
            projects: [
                {
                    extends: true,
                    test: {
                        name: 'unit',
                        include: ['tests/**/*.test.ts'],
                        exclude: ['tests/**/*.timing.test.ts'],
                    },
                },
                // Timings are compared within one process that has the processor to itself.
                {
                    extends: true,
                    test: {
                        name: 'timing',
                        include: ['tests/**/*.timing.test.ts'],
                        pool: 'forks',
                        maxWorkers: 1,
                        // A later group: it starts once the unit tests have finished.
                        sequence: { groupOrder: 1 },
                    },
                },
            ],
        },
    };
});
