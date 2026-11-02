<script lang="ts">
  import type { Snippet } from "svelte";
  import { t } from "../i18n.svelte";
  import { ui } from "../ui.svelte";

  let {
    title,
    onclose,
    children,
    footer,
    wide = false,
  }: {
    title: string;
    onclose: () => void;
    children?: Snippet;
    footer?: Snippet;
    wide?: boolean;
  } = $props();

  // Blocca le scorciatoie globali (Esc) finché la modale è aperta.
  $effect(() => {
    ui.modalDepth += 1;
    return () => {
      ui.modalDepth = Math.max(0, ui.modalDepth - 1);
    };
  });
</script>

<div
  class="modal-backdrop"
  role="presentation"
  onclick={(event) => {
    if (event.target === event.currentTarget) onclose();
  }}
>
  <div
    class="modal"
    style={wide ? "width:min(920px, 100%)" : ""}
    role="dialog"
    aria-modal="true"
    aria-label={title}
    tabindex="-1"
    onclick={(event) => event.stopPropagation()}
    onkeydown={(event) => event.stopPropagation()}
  >
    <div class="modal__head">
      <h2>{title}</h2>
      <span class="spacer"></span>
      <button class="btn btn--ghost btn--sm" onclick={onclose} aria-label={t("common.close")}>
        ✕
      </button>
    </div>
    <div class="modal__body">
      {@render children?.()}
    </div>
    {#if footer}
      <div class="modal__foot">
        {@render footer()}
      </div>
    {/if}
  </div>
</div>

<svelte:window
  onkeydown={(event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onclose();
    }
  }}
/>
