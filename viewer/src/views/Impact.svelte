<script lang="ts">
    import Icon from '../components/Icon.svelte';
    import type { MessageKey } from '../locales/en-GB';
    import { i18n, t } from '../lib/i18n.svelte';
    import Capped from './Capped.svelte';
    import { groupBy, list } from './result';
    import Shell from './Shell.svelte';
    import type { ViewState } from './state.svelte';
    import Verified from './Verified.svelte';

    interface Item {
        id: string;
        kind: string;
        label: string;
        path?: string;
        description?: string;
        depth?: number;
        via?: string;
        transform?: string;
        verified?: string;
        role?: string;
        through?: string;
        command?: string;
        status?: string;
        relation_kind?: string;
    }

    let { state }: { state: ViewState } = $props();

    // The sections of an impact result, in the order the text result gives them.
    const SECTIONS: [string, MessageKey][] = [
        ['relations', 'view.section.relations'],
        ['joins_and_filters', 'view.section.joins'],
        ['tests', 'view.section.tests'],
        ['exposures', 'view.section.exposures'],
        ['tasks', 'view.section.tasks'],
        ['terms', 'view.section.terms'],
    ];
</script>

{#snippet item(entry: Item)}
    <span class="item"
        ><Icon kind={entry.kind} size={16} />{#if entry.kind === 'term'}<b class="name">{entry.label}</b
            >{:else}<code class="name">{entry.label}</code>{/if}{#if entry.role === 'reference'}<span
                class="badge quiet">{t('view.role.reference')}</span
            >{/if}{#if entry.status}<span class="badge quiet status-{entry.status}">{entry.status}</span
            >{/if}</span
    >
    {#if entry.through || entry.verified}<span class="note"
            >{#if entry.through}{t('view.through', { task: entry.through })}{/if}{#if entry.verified}<Verified
                    result={entry.verified}
                />{/if}</span
        >{/if}
    {#if entry.description}<span class="description">{entry.description}</span>{/if}
    {#if entry.path}<span class="path">{entry.path}</span>{/if}
{/snippet}

{#snippet column(entry: Item)}
    <span class="item"><code class="name">{entry.label}</code></span>
    <span class="step-how">
        {#if entry.transform}<span class="transform-badge t-{entry.transform}"
                >{entry.transform === 'direct'
                    ? t('view.transform.direct')
                    : t('view.transform.transformed')}</span
            >{/if}
        {#if entry.verified}<Verified result={entry.verified} />{/if}
        {#if entry.through}<span class="note">{t('view.through', { task: entry.through })}</span>{/if}
    </span>
    {#if entry.description}<span class="description">{entry.description}</span>{/if}
{/snippet}

<Shell {state} tool={t('view.impact')}>
    {#snippet body(data)}
        {@const target = (data.target ?? {}) as Item}
        {@const columns = list<Item>(data.columns)}
        {@const steps = groupBy(columns, c => c.depth ?? 1).sort((a, b) => a[0] - b[0])}
        {@const total = [...SECTIONS.map(([key]) => key), 'columns'].reduce(
            (n, key) => n + list(data[key]).length,
            0,
        )}
        {@const entities = Object.entries((data.entities ?? {}) as Record<string, string[]>)}
        <header class="view-head">
            <h1 id="view-title"><Icon kind={target.kind ?? 'column'} /><code>{target.label ?? ''}</code></h1>
            {#if target.path}<p class="path">{target.path}</p>{/if}
        </header>
        <ul class="stats" aria-label={t('view.impact.counts')}>
            {#each [['columns', 'view.section.columns'] as [string, MessageKey], ...SECTIONS] as [key, name] (key)}
                {@const count = list(data[key]).length}
                {#if count}<li><b>{i18n.number(count)}</b><span>{t(name)}</span></li>{/if}
            {/each}
        </ul>
        {#if total === 0}
            <p class="lead">{t('view.impact.nothing')}</p>
        {/if}
        {#if columns.length}
            <section aria-labelledby="steps-title">
                <h2 id="steps-title">{t('view.impact.steps')}</h2>
                <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                <ol class="lanes" tabindex="0" aria-label={t('view.impact.steps')}>
                    <li class="lane start">
                        <h3>{t('view.impact.start')}</h3>
                        <ul class="capped cards">
                            <li><span class="item"><code class="name">{target.label ?? ''}</code></span></li>
                        </ul>
                    </li>
                    {#each steps as [depth, entries] (depth)}
                        <li class="lane">
                            <h3>{t('view.impact.step', { step: depth })}</h3>
                            <Capped
                                items={entries}
                                row={column}
                                kind="cards"
                                label={t('view.impact.step', { step: depth })}
                            />
                        </li>
                    {/each}
                </ol>
            </section>
        {/if}
        {#each SECTIONS as [key, name] (key)}
            {@const entries = list<Item>(data[key])}
            {#if entries.length}
                <section aria-labelledby="section-{key}">
                    <h2 id="section-{key}">
                        {t(name)} <span class="count">{i18n.number(entries.length)}</span>
                    </h2>
                    <Capped items={entries} row={item} label={t(name)} />
                </section>
            {/if}
        {/each}
        {#if entities.length}
            <section aria-labelledby="section-entities">
                <h2 id="section-entities">{t('view.section.entities')}</h2>
                <ul class="chips-row">
                    {#each entities as [entity, relations] (entity)}
                        <li class="badge quiet">
                            <Icon kind="entity" size={16} />{entity}<span class="note"
                                >{list<string>(relations).join(', ')}</span
                            >
                        </li>
                    {/each}
                </ul>
            </section>
        {/if}
    {/snippet}
</Shell>
