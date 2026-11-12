<script lang="ts">
    import Code from '../components/Code.svelte';
    import Icon from '../components/Icon.svelte';
    import { t } from '../lib/i18n.svelte';
    import Capped from './Capped.svelte';
    import { list, text } from './result';
    import Shell from './Shell.svelte';
    import type { ViewState } from './state.svelte';
    import Verified from './Verified.svelte';

    interface Step {
        column: string;
        label: string;
        relation_kind?: string;
        expression?: string;
        conditions?: string[];
        inputs?: string[];
        verified?: string[];
    }
    interface Term {
        name: string;
        status?: string;
        kind?: string;
        owner?: string;
        definition?: string;
        grain?: string;
        unit?: string;
        currency?: string;
        filters?: string[];
        english?: string[];
    }
    interface English {
        label?: string;
        english?: string | null;
        sentence?: string | null;
        complete?: boolean;
        grain?: string;
        rows?: { relation: string; conditions?: string[]; english?: string[] }[];
        terms?: Term[];
        names?: { wording: string; column: string }[];
        verbatim?: string[];
    }

    let { state }: { state: ViewState } = $props();

    /** Each condition's plain-English wording, from the english part of the result. */
    function wordings(english: English | undefined): Map<string, string> {
        const out = new Map<string, string>();
        for (const row of english?.rows ?? [])
            (row.conditions ?? []).forEach((condition, i) => {
                const wording = row.english?.[i];
                if (wording) out.set(condition, wording);
            });
        return out;
    }

    /** What verify found for the step's edges, each result once. The results are not in the
     * order of the inputs (an edge verify left unmarked has none), so they belong to the step:
     * confirmed alone when every edge was, and otherwise every result there was. */
    function verdicts(entry: Step): string[] {
        return [...new Set(entry.verified ?? [])];
    }

    /** The plain-English sentence, as the command prints it: the sentence, or the formula worded.
     * Both are English whatever the page's language, as the wording itself is. */
    function sentence(english: English | undefined, label: string): string | undefined {
        if (text(english?.sentence)) return english?.sentence ?? undefined;
        if (text(english?.english)) return `${label} is ${english?.english ?? ''}.`;
        return undefined;
    }
</script>

<Shell {state} tool={t('view.explain')}>
    {#snippet body(data)}
        {@const english = data.english as English | undefined}
        {@const worded = wordings(english)}
        {@const said = sentence(english, String(data.label ?? ''))}
        {@const versions = [
            ...(text(data.composed)
                ? [{ name: t('view.explain.composed'), text: data.composed as string }]
                : []),
            ...(text(data.expression) && data.expression !== data.composed
                ? [{ name: t('view.explain.own'), text: data.expression as string }]
                : []),
        ]}
        <header class="view-head">
            <h1 id="view-title">
                <Icon kind="column" /><code>{data.label ?? ''}</code>{#if text(data.data_type)}<span
                        class="badge quiet">{data.data_type}</span
                    >{/if}
            </h1>
            {#if text(data.description)}<p class="description">{data.description}</p>{/if}
        </header>
        {#if said}<p class="lead sentence">{said}</p>{/if}
        <section aria-labelledby="formula-title">
            <h2 id="formula-title">{t('view.explain.formula')}</h2>
            {#if versions.length}
                <Code {versions} label={t('view.explain.formula')} />
            {:else}
                <p class="note">{t('view.explain.source')}</p>
            {/if}
            {#if data.complete === false || english?.complete === false}<p class="warn">
                    {t('view.explain.incomplete')}
                </p>{/if}
            {#if list(data.sources).length}
                <h3>{t('view.explain.sources')}</h3>
                <ul class="chips-row">
                    {#each list<string>(data.sources) as source (source)}
                        <li class="badge quiet"><code>{source}</code></li>
                    {/each}
                </ul>
            {/if}
            {#if text(english?.grain)}<p class="note">
                    {t('view.explain.grain', { grain: english?.grain ?? '' })}
                </p>{/if}
            {#if english?.verbatim?.length}
                <div class="verbatim">
                    <p class="warn">{t('view.explain.verbatim')}</p>
                    <ul class="conditions-list">
                        {#each english.verbatim as part, i (i)}<li><code>{part}</code></li>{/each}
                    </ul>
                </div>
            {/if}
        </section>
        {#if list(data.steps).length}
            <section aria-labelledby="steps-title">
                <h2 id="steps-title">{t('view.explain.steps')}</h2>
                {#snippet step(entry: Step, i: number)}
                    <div class="step">
                        <p class="item">
                            <span class="step-number" aria-hidden="true">{i + 1}</span><code class="name"
                                >{entry.label}</code
                            >{#if entry.relation_kind}<span class="badge quiet">{entry.relation_kind}</span
                                >{/if}{#each verdicts(entry) as result (result)}<Verified {result} />{/each}
                        </p>
                        {#if entry.expression}
                            <pre class="formula-text"><code>{entry.expression}</code></pre>
                        {:else}
                            <p class="note">{t('view.explain.source')}</p>
                        {/if}
                        {#if entry.inputs?.length}
                            <p class="item wrap">
                                <span class="label">{t('view.explain.reads')}</span>
                                {#each entry.inputs as input, j (j)}
                                    <span class="badge quiet"><code>{input}</code></span>
                                {/each}
                            </p>
                        {/if}
                        {#if entry.conditions?.length}
                            <ul class="conditions-list" aria-label={t('view.explain.rows')}>
                                {#each entry.conditions as condition, j (j)}
                                    <li>
                                        <code>{condition}</code>
                                        {#if worded.get(condition)}<span class="wording"
                                                >{worded.get(condition)}</span
                                            >{/if}
                                    </li>
                                {/each}
                            </ul>
                        {/if}
                    </div>
                {/snippet}
                <Capped
                    items={list<Step>(data.steps)}
                    row={step}
                    kind="steps"
                    label={t('view.explain.steps')}
                />
            </section>
        {/if}
        {#if english?.terms?.length}
            <section aria-labelledby="terms-title">
                <h2 id="terms-title">{t('view.section.terms')}</h2>
                <ul class="capped rows" aria-label={t('view.section.terms')}>
                    {#each english.terms as term, i (i)}
                        <li>
                            <span class="item"
                                ><Icon kind="term" size={16} /><b>{term.name}</b>{#if term.status}<span
                                        class="badge quiet status-{term.status}">{term.status}</span
                                    >{/if}{#if term.kind}<span class="badge quiet">{term.kind}</span
                                    >{/if}</span
                            >
                            {#if term.definition}<span class="description">{term.definition}</span>{/if}
                            {#if term.owner}<span class="note"
                                    >{t('view.explain.owner', { owner: term.owner })}</span
                                >{/if}
                            {#if term.unit}<span class="note"
                                    >{t('view.explain.unit', {
                                        unit: term.currency ? `${term.unit} (${term.currency})` : term.unit,
                                    })}</span
                                >{/if}
                            {#if term.grain}<span class="note"
                                    >{t('view.explain.grain', { grain: term.grain })}</span
                                >{/if}
                            {#if term.filters?.length}
                                <ul class="conditions-list" aria-label={t('view.explain.requires')}>
                                    {#each term.filters as filter, j (j)}
                                        <li>
                                            <span class="label">{t('view.explain.requires')}</span><code
                                                >{filter}</code
                                            >
                                            {#if term.english?.[j]}<span class="wording"
                                                    >{term.english[j]}</span
                                                >{/if}
                                        </li>
                                    {/each}
                                </ul>
                            {/if}
                        </li>
                    {/each}
                </ul>
            </section>
        {/if}
        {#if english?.names?.length}
            <section aria-labelledby="names-title">
                <h2 id="names-title">{t('view.explain.names')}</h2>
                <ul class="capped rows" aria-label={t('view.explain.names')}>
                    {#each english.names as name, i (i)}
                        <li>
                            <span class="wording">{name.wording}</span><code class="path">{name.column}</code>
                        </li>
                    {/each}
                </ul>
            </section>
        {/if}
    {/snippet}
</Shell>
