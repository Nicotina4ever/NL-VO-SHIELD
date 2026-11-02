<script lang="ts">
  import { onMount } from "svelte";
  import {
    drawWaveform,
    loadWaveform,
    peakDbAt,
    readWaveColors,
    type WaveColors,
  } from "../waveform";
  import type { WaveformData } from "../types";
  import { ui } from "../ui.svelte";
  import { t } from "../i18n.svelte";
  import { formatDuration, formatNumber } from "../format";

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
  let shell = $state<HTMLDivElement | undefined>(undefined);
  let data = $state<WaveformData | null>(null);
  let loading = $state(false);
  let errorText = $state<string | null>(null);
  let colors = $state<WaveColors>(readWaveColors());
  let hover = $state<number | null>(null);
  let hoverClientX = $state(0);
  let dragging = $state(false);
  let token = 0;

  const duration = $derived(data?.durationSec ?? 0);
  const hoverSeconds = $derived(hover != null ? hover * duration : null);
  const hoverDb = $derived(hover != null ? peakDbAt(data, hover) : null);

  function draw(): void {
    if (!canvas) return;
    drawWaveform(canvas, data, {
      colors,
      durationSec: duration,
      playhead,
      hover,
    });
  }

  function fractionFromEvent(event: PointerEvent | MouseEvent): number {
    if (!canvas) return 0;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0) return 0;
    return Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  }

  function seek(event: PointerEvent | MouseEvent): void {
    if (!interactive || !onseek || !data?.supported) return;
    onseek(fractionFromEvent(event));
  }

  function onPointerDown(event: PointerEvent): void {
    if (!interactive) return;
    dragging = true;
    (event.currentTarget as HTMLCanvasElement).setPointerCapture?.(event.pointerId);
    seek(event);
  }

  function onPointerMove(event: PointerEvent): void {
    if (!interactive) return;
    hover = fractionFromEvent(event);
    hoverClientX = event.clientX;
    if (dragging) seek(event);
  }

  function onPointerUp(event: PointerEvent): void {
    if (!dragging) return;
    dragging = false;
    (event.currentTarget as HTMLCanvasElement).releasePointerCapture?.(event.pointerId);
  }

  function onPointerLeave(): void {
    hover = null;
    dragging = false;
  }

  function handleKey(event: KeyboardEvent): void {
    if (!interactive || !onseek) return;
    const current = playhead ?? 0;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      onseek(Math.min(1, current + (event.shiftKey ? 0.1 : 0.02)));
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      onseek(Math.max(0, current - (event.shiftKey ? 0.1 : 0.02)));
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

  // ridisegna su dati, playhead, hover, tema, DPI, resize
  $effect(() => {
    void data;
    void playhead;
    void hover;
    void ui.theme;
    colors = readWaveColors();
    draw();
  });

  onMount(() => {
    if (!shell) return;
    const observer = new ResizeObserver(() => draw());
    observer.observe(shell);
    const media = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
    const onDpi = () => draw();
    media.addEventListener?.("change", onDpi);
    return () => {
      observer.disconnect();
      media.removeEventListener?.("change", onDpi);
    };
  });
</script>

<div class="wave-shell {tall ? 'wave-shell--tall' : ''}" bind:this={shell}>
  {#if label}
    <div class="wave-shell__meta">
      <span class="wave-shell__label">{label}</span>
      {#if data?.supported}
        <span class="wave-shell__spec tnum">
          {data.sampleRate} Hz<span class="sep">·</span>{data.channels} ch<span class="sep">·</span>{formatDuration(data.durationSec)}
        </span>
      {/if}
    </div>
  {/if}

  <div class="wave-shell__canvas">
    <canvas
      bind:this={canvas}
      class="wave"
      style={interactive ? "cursor:crosshair" : ""}
      role={interactive ? "slider" : undefined}
      tabindex={interactive ? 0 : undefined}
      aria-label={interactive ? `${label || t("protect.preview")} — ${t("player.seek")}` : undefined}
      aria-valuemin={interactive ? 0 : undefined}
      aria-valuemax={interactive ? 100 : undefined}
      aria-valuenow={interactive ? Math.round((playhead ?? 0) * 100) : undefined}
      aria-hidden={interactive ? undefined : "true"}
      onpointerdown={onPointerDown}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={onPointerUp}
      onpointerleave={onPointerLeave}
      onkeydown={handleKey}
    ></canvas>

    {#if !path}
      <div class="wave-empty">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M3 12h2l1.6-5.4L9 17.4 11.4 4l2.2 14.6L15.8 8 17.4 12H21"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <span class="wave-empty__title">{t("player.emptyWaveform")}</span>
        <span class="wave-empty__hint">{t("player.emptyWaveformHint")}</span>
      </div>
    {:else if loading}
      <div class="wave-loading">
        <span class="spinner"></span>
        <span class="small muted">{t("player.decoding")}</span>
      </div>
    {:else if errorText}
      <div class="wave-error">
        <span class="small">{errorText}</span>
      </div>
    {/if}

    {#if interactive && hover != null && data?.supported}
      <div
        class="wave-tip tnum"
        style={`left:${Math.max(8, Math.min((shell?.clientWidth ?? 320) - 118, hoverClientX - (shell?.getBoundingClientRect().left ?? 0) + 12))}px`}
      >
        <span>{formatDuration(hoverSeconds ?? 0)}</span>
        <span class="sep">·</span>
        <span>{hoverDb != null ? `${formatNumber(hoverDb, 1)} dBFS` : "−∞ dBFS"}</span>
      </div>
    {/if}
  </div>

  <span class="sr-only">
    {label || t("protect.preview")}
    {#if data?.supported}
      · {data.sampleRate} Hz · {data.channels} ch · {formatDuration(data.durationSec)}
    {/if}
  </span>
</div>
