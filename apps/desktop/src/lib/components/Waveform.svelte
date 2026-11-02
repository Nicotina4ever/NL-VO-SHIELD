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
    playhead = null,
    onseek = undefined,
    interactive = false,
  }: {
    path?: string | null;
    tall?: boolean;
    label?: string;
    playhead?: number | null;
    onseek?: (fraction: number) => void;
    interactive?: boolean;
  } = $props();

  let canvas = $state<HTMLCanvasElement | undefined>(undefined);
  let data = $state<WaveformData | null>(null);
  let loading = $state(false);
  let errorText = $state<string | null>(null);
  let token = 0;

  function draw(): void {
    if (!canvas) return;
    drawWaveform(canvas, data, { playhead });
  }

  function seekFromEvent(event: MouseEvent): void {
    if (!interactive || !onseek) return;
    const target = event.currentTarget as HTMLCanvasElement;
    const rect = target.getBoundingClientRect();
    if (rect.width <= 0) return;
    onseek((event.clientX - rect.left) / rect.width);
  }

  function handleKey(event: KeyboardEvent): void {
    if (!interactive || !onseek) return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      onseek(Math.min(1, (playhead ?? 0) + 0.02));
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      onseek(Math.max(0, (playhead ?? 0) - 0.02));
    } else if (event.key === "Home") {
      event.preventDefault();
      onseek(0);
    } else if (event.key === "End") {
      event.preventDefault();
      onseek(1);
    }
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

  // ridisegna quando cambiano dati, playhead, tema o dimensione della finestra
  $effect(() => {
    void data;
    void playhead;
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
    <canvas
      bind:this={canvas}
      class="wave {tall ? 'wave--tall' : ''}"
      style={interactive ? "cursor:crosshair" : ""}
      role={interactive ? "slider" : undefined}
      tabindex={interactive ? 0 : undefined}
      aria-label={interactive ? `${label || t("protect.preview")} — ${t("player.seek")}` : undefined}
      aria-valuemin={interactive ? 0 : undefined}
      aria-valuemax={interactive ? 100 : undefined}
      aria-valuenow={interactive ? Math.round((playhead ?? 0) * 100) : undefined}
      aria-hidden={interactive ? undefined : "true"}
      onclick={seekFromEvent}
      onkeydown={handleKey}
    ></canvas>
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
