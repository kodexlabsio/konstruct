<script lang="ts">
    import Icon from '../components/Icon.svelte';
    import type { MessageKey } from '../locales/en-GB';
    import { i18n, t } from '../lib/i18n.svelte';
    import Capped from './Capped.svelte';
    import { groupBy, list, text } from './result';
    import Shell from './Shell.svelte';
    import type { ViewState } from './state.svelte';

    interface Against {
        name: string;
        source?: string;
        grade?: string;
        owner?: string | null;
    }
    interface Finding {
        document?: string;
        kind: string;
        name: string;
        verdict: string;
        severity?: string;
        detail?: string;
        expression?: string | null;
        code?: string | null;
        against?: Against[];
        missing_filters?: { filter: string; against: string; by: string }[];
        witness?: string | null;
        error_bound?: number;
        layers?: { document: string; name: string }[];
    }
    interface Layer {
        key: string;
        name: string;
        description?: string;
    }

    let { state }: { state: ViewState } = $props();

    const VERDICTS: [string, MessageKey][] = [
        ['conforms', 'view.verdict.conforms'],
        ['differs', 'view.verdict.differs'],
        ['unbound', 'view.verdict.unbound'],
        ['unparsed', 'view.verdict.unparsed'],
    ];
    const verdicts = new Map(VERDICTS);
    const SEVERITIES: Record<string, MessageKey> = {
        error: 'view.severity.error',
        warning: 'view.severity.warning',
        info: 'view.severity.info',
    };
    const tone = (f: Finding): string =>
        f.verdict === 'conforms'
            ? 'ok'
            : f.severity === 'error' || f.severity === 'warning'
              ? 'warn'
              : 'quiet';
</script>

{#snippet finding(entry: Finding)}
    <article class="finding tone-{tone(entry)}">
        <p class="item">
            <Icon
                kind={entry.kind === 'metric' ? 'usage' : entry.kind === 'key' ? 'link' : 'schema'}
                size={16}
            /><code class="name">{entry.name}</code><span class="badge {tone(entry)}"
                >{verdicts.has(entry.verdict) ? t(verdicts.get(entry.verdict)!) : entry.verdict}</span
            >{#if entry.severity && entry.severity !== 'info'}<span class="badge quiet"
                    >{SEVERITIES[entry.severity] ? t(SEVERITIES[entry.severity]!) : entry.severity}</span
                >{/if}
        </p>
        {#if entry.detail}<p class="message">{entry.detail}</p>{/if}
        {#if entry.layers?.length}
            <p class="note">{entry.layers.map(l => l.name).join(' / ')}</p>
        {/if}
        {#if entry.expression || entry.code}
            <dl class="formulas">
                {#if entry.expression}
                    <div>
                        <dt>{t('view.conform.layer')}</dt>
                        <dd><pre class="formula-text"><code>{entry.expression}</code></pre></dd>
                    </div>
                {/if}
                {#if entry.code}
                    <div>
                        <dt>{t('view.conform.code')}</dt>
                        <dd><pre class="formula-text"><code>{entry.code}</code></pre></dd>
                    </div>
                {/if}
            </dl>
        {/if}
        {#if entry.against?.length}
            <p class="item wrap">
                <span class="label">{t('view.conform.against')}</span>
                {#each entry.against as other, i (i)}
                    <span class="badge quiet"
                        >{other.name}{#if other.source}<span class="note"
                                >{other.source}{other.grade ? `, ${other.grade}` : ''}</span
                            >{/if}</span
                    >
                {/each}
            </p>
        {/if}
        {#each entry.missing_filters ?? [] as missing, i (i)}
            <p class="fix">
                <span class="label">{t('view.conform.missing')}</span><code>{missing.filter}</code>
                <span class="note">{missing.against}: {missing.by}</span>
            </p>
        {/each}
        {#if entry.witness}
            <p class="fix">
                <span class="label">{t('view.conform.witness')}</span><code>{entry.witness}</code>
            </p>
        {/if}
        {#if entry.verdict === 'conforms' && typeof entry.error_bound === 'number'}
            <p class="note">
                {t('view.conform.bound', {
                    bound: entry.error_bound === 0 ? '0' : entry.error_bound.toExponential(1),
                })}
            </p>
        {/if}
    </article>
{/snippet}

<Shell {state} tool={t('view.conform')}>
    {#snippet body(data)}
        {@const layers = list<Layer>(data.documents)}
        {@const findings = list<Finding>(data.findings)}
        {@const summary = (data.summary ?? {}) as Record<string, number>}
        {@const byLayer = new Map(groupBy(findings, f => f.document ?? ''))}
        {@const known = new Set(layers.map(l => l.key))}
        {@const others = findings.filter(f => !known.has(f.document ?? ''))}
        <header class="view-head">
            <h1 id="view-title"><Icon kind="schema" />{t('view.conform.title')}</h1>
        </header>
        <ul class="stats" aria-label={t('view.conform.counts')}>
            <li><b>{i18n.number(summary.metrics ?? 0)}</b><span>{t('view.conform.metrics')}</span></li>
            {#each VERDICTS as [verdict, name] (verdict)}
                <li class="v-{verdict}"><b>{i18n.number(summary[verdict] ?? 0)}</b><span>{t(name)}</span></li>
            {/each}
        </ul>
        {#if !layers.length && !list(data.problems).length}
            <p class="lead">{t('view.conform.nothing')}</p>
        {/if}
        {#each layers as layer, n (layer.key)}
            {@const items = byLayer.get(layer.key) ?? []}
            <section aria-labelledby="layer-{n}">
                <h2 id="layer-{n}">{layer.name}</h2>
                {#if text(layer.description)}<p class="note">{layer.description}</p>{/if}
                <Capped {items} row={finding} kind="findings" label={layer.name} />
            </section>
        {/each}
        {#if others.length}
            <section aria-labelledby="layer-others">
                <h2 id="layer-others">{t('view.conform.between')}</h2>
                <Capped items={others} row={finding} kind="findings" label={t('view.conform.between')} />
            </section>
        {/if}
        {#if list(data.problems).length}
            <section aria-labelledby="problems-title">
                <h2 id="problems-title">{t('view.conform.problems')}</h2>
                <ul class="capped rows" aria-label={t('view.conform.problems')}>
                    {#each list<string>(data.problems) as problem, i (i)}<li>
                            <code>{problem}</code>
                        </li>{/each}
                </ul>
            </section>
        {/if}
    {/snippet}
</Shell>
