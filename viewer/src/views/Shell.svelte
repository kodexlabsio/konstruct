<script lang="ts">
    import type { Snippet } from 'svelte';
    import Icon from '../components/Icon.svelte';
    import { t } from '../lib/i18n.svelte';
    import type { ViewState } from './state.svelte';

    let { state, tool, body }: { state: ViewState; tool: string; body: Snippet<[Record<string, unknown>]> } =
        $props();
    const read = $derived(state.read);
    const shown = $derived(
        state.cancelled !== null
            ? 'cancelled'
            : read === null
              ? 'waiting'
              : 'error' in read
                ? 'error'
                : 'result',
    );
</script>

<main class="view" aria-labelledby="view-title" data-state={shown}>
    <p class="view-tool"><span class="view-mark" aria-hidden="true"></span>Konstruct <span>{tool}</span></p>
    {#if state.cancelled !== null}
        <h1 id="view-title" class="view-state">{t('view.cancelled')}</h1>
        {#if state.cancelled}<p class="note">{state.cancelled}</p>{/if}
    {:else if read === null}
        <h1 id="view-title" class="view-state" role="status">{t('view.waiting')}</h1>
    {:else if 'error' in read}
        <h1 id="view-title" class="view-state">{t('view.error')}</h1>
        <pre class="view-error" role="alert">{read.error}</pre>
    {:else}
        {@render body(read.data)}
    {/if}
    {#if read !== null}
        <p class="untrusted"><Icon kind="warnings" size={16} /><span>{t('view.untrusted')}</span></p>
    {/if}
</main>
