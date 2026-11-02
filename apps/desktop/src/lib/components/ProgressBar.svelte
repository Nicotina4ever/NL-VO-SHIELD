<script lang="ts">
  let {
    value = 0,
    label = "",
    indeterminate = false,
  }: { value?: number; label?: string; indeterminate?: boolean } = $props();

  const pct = $derived(
    Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0)),
  );
</script>

<div class="col" style="gap:5px">
  {#if label}
    <div class="row gap-sm">
      <span class="small muted">{label}</span>
      <span class="spacer"></span>
      <span class="small mono faint">{indeterminate ? "…" : `${pct.toFixed(0)}%`}</span>
    </div>
  {/if}
  <div
    class="progress {indeterminate ? 'progress--indeterminate' : ''}"
    role="progressbar"
    aria-label={label || "progress"}
    aria-valuemin="0"
    aria-valuemax="100"
    aria-valuenow={Math.round(pct)}
  >
    <div
      class="progress__bar"
      style={`width:${indeterminate ? 35 : pct}%`}
    ></div>
  </div>
</div>
