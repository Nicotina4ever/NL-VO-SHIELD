<script lang="ts">
  import { drawWaveform, loadWaveform } from "../waveform";
  import type { WaveformData } from "../types";
  import { ui } from "../ui.svelte";
  import { t } from "../i18n.svelte";
  import { formatDuration } from "../format";

  let {
    path = null,
    tall = false,
    label = "",
  }: { path?: string | null; tall?: boolean; label?: string } = $props();

  let canvas = $state<HTMLCanvasElement | undefined>(undefined);
  let data = $state<WaveformData | null>(null);
  let loading = $state(false);
  let errorText = $state<string | null>(null);
  let token = 0;

  function draw(): void {
    if (!canvas) return;
    drawWaveform(canvas, data);
  }

  $effect(() => {
    const current = path;
    const myToken = ++token;
    if (!current) {
      data = null;
      loading = false;
      errorText = null;
      return;
    }
    loading = true;
    errorText = null;
    loadWaveform(current)
      .then((result) => {
        if (myToken !== token) return;
        data = result;
        loading = false;
        errorText = result.supported ? null : (result.reason ?? t("protect.waveUnsupported"));
      })
      .catch(() => {
        if (myToken !== token) return;
        data = null;
        loading = false;
        errorText = t("protect.waveUnsupported");
      });
  });

  // ridisegna quando cambiano dati, tema o dimensione della finestra
  $effect(() => {
    void data;
    void ui.theme;
    draw();
    const onResize = () => draw();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  });
</script>

<div class="col" style="gap:6px">
  {#if label}
    <div class="row gap-sm">
      <span class="small muted">{label}</span>
      {#if data?.supported}
        <span class="small mono faint">
          {data.sampleRate} Hz · {data.channels} ch · {formatDuration(data.durationSec)}
        </span>
      {/if}
    </div>
  {/if}
  <div style="position:relative">
    <canvas bind:this={canvas} class="wave {tall ? 'wave--tall' : ''}" aria-hidden="true"></canvas>
    <span class="sr-only">
      {label || t("protect.preview")}
      {#if data?.supported}
        · {data.sampleRate} Hz · {data.channels} ch · {formatDuration(data.durationSec)}
      {/if}
    </span>
    {#if loading}
      <div
        class="small muted"
        style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center"
      >
        <span class="spinner"></span>
      </div>
    {/if}
  </div>
  {#if errorText && !loading}
    <div class="small faint">{errorText}</div>
  {/if}
</div>
