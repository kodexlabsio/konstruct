<script lang="ts">
    // Text from the repository with a button to copy it; where the host keeps the clipboard from
    // the view, the button selects the text shown here instead.
    import Command from '../components/Command.svelte';

    let { text, label }: { text: string; label: string } = $props();
    let shown: HTMLElement | undefined = $state();
</script>

<code class="path" bind:this={shown}>{text}</code><Command {text} plain {label} of={shown} />
