<script lang="ts">
    import Icon from '../components/Icon.svelte';
    import { t } from '../lib/i18n.svelte';
    import Capped from './Capped.svelte';
    import Copyable from './Copyable.svelte';
    import { list, text } from './result';
    import Shell from './Shell.svelte';
    import type { ViewState } from './state.svelte';

    interface Finding {
        rule: string;
        message: string;
        path?: string | null;
        fix?: string;
    }

    let { state }: { state: ViewState } = $props();
</script>

{#snippet finding(entry: Finding)}
    <article class="finding">
        <p class="item"><span class="badge warn">{entry.rule}</span></p>
        <p class="message">{entry.message}</p>
        {#if entry.path}
            <p class="item file">
                <span class="label">{t('view.gate.file')}</span><Copyable
                    text={entry.path}
                    label={t('view.copyPath')}
                />
            </p>
        {/if}
        {#if entry.fix}<p class="fix">
                <span class="label">{t('view.gate.fix')}</span><span class="fix-text">{entry.fix}</span>
            </p>{/if}
    </article>
{/snippet}

<Shell {state} tool={t('view.gate')}>
    {#snippet body(data)}
        {@const findings = list<Finding>(data.findings)}
        {@const passed = data.passed === true}
        <header class="view-head verdict {passed ? 'passed' : 'failed'}">
            <h1 id="view-title">
                <Icon kind={passed ? 'check' : 'conflicts'} />{passed
                    ? t('view.gate.passed')
                    : t('view.gate.failed')}
            </h1>
            <p class="lead">
                {passed ? t('view.gate.clear') : t('view.gate.findings', { count: findings.length })}
            </p>
            {#if text(data.base)}<p class="note">{t('view.gate.base', { base: data.base as string })}</p>{/if}
        </header>
        {#if findings.length}
            <section aria-labelledby="findings-title">
                <h2 id="findings-title" class="sr-only">{t('view.gate.findingsTitle')}</h2>
                <Capped items={findings} row={finding} kind="findings" label={t('view.gate.findingsTitle')} />
            </section>
        {/if}
        {#if text(data.laws_from)}<p class="note">
                {t('view.gate.laws', { laws: data.laws_from as string })}
            </p>{/if}
    {/snippet}
</Shell>
