<script lang="ts" generics="T">
    // A long list drawn a page at a time: a result can hold thousands of items (the impact of a
    // model in a large project), and the text result beside the view always has every one.
    import type { Snippet } from 'svelte';
    import { t } from '../lib/i18n.svelte';
    import { PAGE } from './result';

    let {
        items,
        row,
        label,
        page = PAGE,
        kind = 'rows',
    }: { items: T[]; row: Snippet<[T, number]>; label: string; page?: number; kind?: string } = $props();
    let count = $state(0);
    const shown = $derived(Math.min(items.length, count || page));
</script>

<ul class="capped {kind}" aria-label={label}>
    {#each items.slice(0, shown) as item, i (i)}
        <li>{@render row(item, i)}</li>
    {/each}
</ul>
{#if shown < items.length}
    <p class="more">
        <span class="note">{t('view.shown', { shown, total: items.length })}</span>
        <button type="button" class="copy" onclick={() => (count = shown + page)}
            >{t('view.more', { count: Math.min(page, items.length - shown) })}</button
        >
    </p>
{/if}
