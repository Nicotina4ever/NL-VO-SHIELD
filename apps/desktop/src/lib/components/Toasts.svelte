<script lang="ts">
  import { dismissToast, ui } from "../ui.svelte";
  import { t } from "../i18n.svelte";
</script>

{#if ui.toasts.length > 0}
  <div class="toasts" aria-live="polite" aria-atomic="false">
    {#each ui.toasts as item (item.id)}
      <div
        class="toast {item.kind === 'ok'
          ? 'toast--ok'
          : item.kind === 'err'
            ? 'toast--err'
            : item.kind === 'warn'
              ? 'toast--warn'
              : ''}"
        role="status"
      >
        <div class="toast__body">
          {#if item.title}
            <div class="toast__title">{item.title}</div>
          {/if}
          <div>{item.message}</div>
        </div>
        <button
          class="btn btn--ghost btn--sm"
          onclick={() => dismissToast(item.id)}
          aria-label={t("common.close")}
          title={t("common.close")}
        >
          ✕
        </button>
      </div>
    {/each}
  </div>
{/if}
