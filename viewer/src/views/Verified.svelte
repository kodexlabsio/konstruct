<script lang="ts">
    // What `konstruct verify` found for an edge: confirmed in the data, or anything else, which
    // must not look like a confirmation.
    import { t } from '../lib/i18n.svelte';

    let { result }: { result: string } = $props();
</script>

<span class="verified {result === 'confirmed' ? 'ok' : 'warn'}">{t('view.verify', { result })}</span>
