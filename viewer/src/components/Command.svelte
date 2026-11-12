<script lang="ts">
    import { t } from '../lib/i18n.svelte';

    let {
        text,
        plain = false,
        label,
        of,
    }: { text: string; plain?: boolean; label?: string; of?: HTMLElement | undefined } = $props();
    let code: HTMLElement | undefined = $state();
    let copied = $state(false);
    // Where the clipboard is out of reach (a file:// page, a host's frame that does not grant it),
    // the text is selected for the keyboard instead, and the button says so for as long as it
    // shows that text. ``of`` is an element on the page that shows exactly the text, selected so
    // the reader sees it; without one the hidden copy is, so nothing else (a code block's line
    // numbers) gets into the copy.
    let selected = $state(false);
    $effect.pre(() => {
        void text;
        selected = false;
    });

    function selectText(): void {
        const target = of ?? code;
        if (!target) return;
        selected = true;
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
    }

    function copy(): void {
        const done = (): void => {
            copied = true;
            setTimeout(() => (copied = false), 1400);
        };
        // file:// pages are not a secure context, so the clipboard API may be missing: select instead.
        if (navigator.clipboard && window.isSecureContext)
            navigator.clipboard.writeText(text).then(done, selectText);
        else selectText();
    }
</script>

{#if plain}
    <code class="sr-only" bind:this={code}>{text}</code>
    <button type="button" class="copy" onclick={copy}
        ><span aria-live="polite"
            >{copied ? t('code.copied') : selected ? t('code.selected') : (label ?? t('code.copy'))}</span
        ></button
    >
{:else}
    <div class="command">
        <code bind:this={code}>{text}</code>
        <button
            type="button"
            class="copy"
            onclick={copy}
            aria-label={t('code.copyCommand', { command: text })}
            ><span aria-live="polite"
                >{copied ? t('code.copied') : selected ? t('code.selected') : t('code.copy')}</span
            ></button
        >
    </div>
{/if}
