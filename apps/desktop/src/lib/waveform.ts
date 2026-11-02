/**
 * Renderer della forma d'onda (canvas, stile studio).
 *
 * Disegno a PATH CONTINUI, non a colonne:
 * - contorno superiore sui picchi max, ritorno sui min, unico poligono specchiato
 *   riempito (lineJoin round), poi tratto di bordo 1px per definire il picco;
 * - sopra, un secondo poligono RMS più chiaro (inviluppo);
 * - porzione riprodotta in accent smorzato, resto desaturato (clip sul percorso);
 * - reticolo a secondi con etichette, linea di centro, hover, playhead + maniglia;
 * - campionamento ≥2 punti per pixel, coordinate scalate per DPR (1x/2x/3x).
 */
import type { WaveformData } from "./types";
import { engineApi } from "./ipc";

export interface WaveColors {
  bg: string;
  grid: string;
  gridLabel: string;
  center: string;
  peakIdle: string;
  peakPlayed: string;
  edgeIdle: string;
  edgePlayed: string;
  rmsIdle: string;
  rmsPlayed: string;
  playhead: string;
  handle: string;
  hover: string;
  text: string;
}

const FALLBACK: WaveColors = {
  bg: "#121116",
  grid: "#232029",
  gridLabel: "#6f6880",
  center: "#2c2836",
  peakIdle: "rgba(154,147,171,0.28)",
  peakPlayed: "rgba(186,38,108,0.34)",
  edgeIdle: "rgba(192,186,204,0.75)",
  edgePlayed: "rgba(214,84,138,0.95)",
  rmsIdle: "rgba(154,147,171,0.10)",
  rmsPlayed: "rgba(186,38,108,0.14)",
  playhead: "#ede9f2",
  handle: "#ba266c",
  hover: "rgba(154,147,171,0.45)",
  text: "#9a93ab",
};

function token(styles: CSSStyleDeclaration, name: string, fallback: string): string {
  const value = styles.getPropertyValue(name).trim();
  return value || fallback;
}

/** Legge i colori dai token CSS (tema corrente). */
export function readWaveColors(): WaveColors {
  if (typeof document === "undefined") return FALLBACK;
  const styles = getComputedStyle(document.documentElement);
  const muted = token(styles, "--nl-text-muted", "#9a93ab");
  const bright = token(styles, "--nl-text-2", "#c9c4d4");
  const rosa = token(styles, "--nl-accent", "#ba266c");
  return {
    bg: token(styles, "--nl-wave-bg", "#121116"),
    grid: token(styles, "--nl-wave-grid", "#232029"),
    gridLabel: token(styles, "--nl-wave-label", muted),
    center: token(styles, "--nl-border", "#2c2836"),
    peakIdle: `color-mix(in srgb, ${muted} 26%, transparent)`,
    peakPlayed: `color-mix(in srgb, ${rosa} 32%, transparent)`,
    edgeIdle: `color-mix(in srgb, ${bright} 70%, transparent)`,
    edgePlayed: `color-mix(in srgb, ${rosa} 92%, transparent)`,
    rmsIdle: `color-mix(in srgb, ${muted} 10%, transparent)`,
    rmsPlayed: `color-mix(in srgb, ${rosa} 13%, transparent)`,
    playhead: token(styles, "--nl-text", "#ede9f2"),
    handle: rosa,
    hover: `color-mix(in srgb, ${muted} 45%, transparent)`,
    text: muted,
  };
}

export interface WaveOptions {
  colors: WaveColors;
  durationSec: number;
  playhead?: number | null;
  hover?: number | null;
  timeLabels?: boolean;
}

interface Plot {
  x: number;
  y: number;
  w: number;
  h: number;
}

function pickStep(duration: number): number {
  const steps = [0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const target = duration / 8;
  for (const step of steps) {
    if (step >= target) return step;
  }
  return steps[steps.length - 1];
}

function formatClock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

interface Envelope {
  max: Float32Array;
  min: Float32Array;
  rms: Float32Array;
  count: number;
}

/**
 * Campiona l'inviluppo a `columns * samplesPerColumn` punti (≥2 per pixel CSS),
 * aggregando i bucket del motore con min/max e media RMS.
 */
function buildEnvelope(data: WaveformData, columns: number, samplesPerColumn: number): Envelope {
  const buckets = data.peaks.min.length;
  const count = Math.max(2, Math.min(columns * samplesPerColumn, Math.max(columns * 2, buckets)));
  const max = new Float32Array(count);
  const min = new Float32Array(count);
  const rms = new Float32Array(count);
  const rmsSource = data.rms ?? [];
  for (let index = 0; index < count; index += 1) {
    const start = Math.floor((index / count) * buckets);
    const end = Math.max(start, Math.floor(((index + 1) / count) * buckets) - 1);
    let low = 0;
    let high = 0;
    let rmsAcc = 0;
    let rmsCount = 0;
    for (let bucket = start; bucket <= end && bucket < buckets; bucket += 1) {
      const maxValue = data.peaks.max[bucket] ?? 0;
      const minValue = data.peaks.min[bucket] ?? 0;
      if (maxValue > high) high = maxValue;
      if (minValue < low) low = minValue;
      if (bucket < rmsSource.length) {
        rmsAcc += rmsSource[bucket] ?? 0;
        rmsCount += 1;
      }
    }
    max[index] = high;
    min[index] = low;
    rms[index] = rmsCount > 0 ? rmsAcc / rmsCount : 0;
  }
  return { max, min, rms, count };
}

/** Poligono specchiato continuo: max da sinistra a destra, min al ritorno. */
function envelopePath(
  ctx: CanvasRenderingContext2D,
  envelope: Envelope,
  plot: Plot,
  mid: number,
  half: number,
  from: number,
  to: number,
  rmsOnly: boolean,
): void {
  const count = envelope.count;
  const start = Math.max(0, Math.min(count - 1, from));
  const end = Math.max(start, Math.min(count - 1, to));
  const step = (plot.w / Math.max(1, count - 1));
  ctx.beginPath();
  for (let index = start; index <= end; index += 1) {
    const value = rmsOnly
      ? Math.min(half, (envelope.rms[index] ?? 0) * half)
      : Math.max(0.001, envelope.max[index] ?? 0) * half;
    const x = plot.x + index * step;
    const y = mid - value;
    if (index === start) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  for (let index = end; index >= start; index -= 1) {
    const value = rmsOnly
      ? Math.min(half, (envelope.rms[index] ?? 0) * half)
      : Math.min(-0.001, envelope.min[index] ?? 0) * half;
    const x = plot.x + index * step;
    const y = mid - value;
    ctx.lineTo(x, y);
  }
  ctx.closePath();
}

function strokeOutline(
  ctx: CanvasRenderingContext2D,
  envelope: Envelope,
  plot: Plot,
  mid: number,
  half: number,
  from: number,
  to: number,
  upper: boolean,
): void {
  const count = envelope.count;
  const start = Math.max(0, Math.min(count - 1, from));
  const end = Math.max(start, Math.min(count - 1, to));
  const step = plot.w / Math.max(1, count - 1);
  ctx.beginPath();
  for (let index = start; index <= end; index += 1) {
    const raw = upper ? (envelope.max[index] ?? 0) : (envelope.min[index] ?? 0);
    const value = upper ? Math.max(0.001, raw) : Math.min(-0.001, raw);
    const x = plot.x + index * step;
    const y = mid - value * half;
    if (index === start) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function columnAtX(plot: Plot, count: number, x: number): number {
  if (plot.w <= 0) return 0;
  return Math.round(((x - plot.x) / plot.w) * Math.max(1, count - 1));
}

/** Disegna la forma d'onda sul canvas, adattandosi al DPI. */
export function drawWaveform(
  canvas: HTMLCanvasElement,
  data: WaveformData | null,
  options: WaveOptions,
): void {
  const dpr = Math.min(3, window.devicePixelRatio || 1);
  const cssWidth = Math.max(80, canvas.clientWidth || 320);
  const cssHeight = Math.max(48, canvas.clientHeight || 96);
  const pixelWidth = Math.round(cssWidth * dpr);
  const pixelHeight = Math.round(cssHeight * dpr);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  // coordinate CSS, scalate sui device pixel (1x/2x/3x)
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const colors = options.colors;
  ctx.fillStyle = colors.bg;
  ctx.fillRect(0, 0, cssWidth, cssHeight);

  const showLabels = options.timeLabels !== false;
  const padding = { left: 8, right: 8, top: 8, bottom: showLabels ? 18 : 8 };
  const plot: Plot = {
    x: padding.left,
    y: padding.top,
    w: Math.max(8, cssWidth - padding.left - padding.right),
    h: Math.max(8, cssHeight - padding.top - padding.bottom),
  };
  const mid = plot.y + plot.h / 2;
  const half = plot.h / 2 - 1;
  const duration = Math.max(0.001, options.durationSec || data?.durationSec || 0);

  if (showLabels) {
    const step = pickStep(duration);
    const divisions = Math.floor(duration / step);
    ctx.font =
      "500 9.5px 'Segoe UI Variable Text', 'Segoe UI', system-ui, -apple-system, sans-serif";
    ctx.textBaseline = "top";
    for (let index = 0; index <= divisions; index += 1) {
      const t = index * step;
      const x = plot.x + (t / duration) * plot.w;
      ctx.strokeStyle = colors.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, plot.y);
      ctx.lineTo(Math.round(x) + 0.5, plot.y + plot.h);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, plot.y + plot.h + 1);
      ctx.lineTo(Math.round(x) + 0.5, plot.y + plot.h + 4);
      ctx.stroke();
      const label = formatClock(t);
      const textWidth = ctx.measureText(label).width;
      const labelX = Math.max(
        plot.x + textWidth / 2,
        Math.min(plot.x + plot.w - textWidth / 2, x),
      );
      ctx.fillStyle = colors.gridLabel;
      ctx.textAlign = "center";
      ctx.fillText(label, labelX, plot.y + plot.h + 6);
    }
  }

  ctx.strokeStyle = colors.center;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(plot.x, Math.round(mid) + 0.5);
  ctx.lineTo(plot.x + plot.w, Math.round(mid) + 0.5);
  ctx.stroke();

  const peaks = data?.peaks;
  if (!data?.supported || !peaks || peaks.min.length === 0) return;

  const envelope = buildEnvelope(data, Math.floor(plot.w), 2);
  const last = envelope.count - 1;
  const playheadX =
    options.playhead != null && Number.isFinite(options.playhead)
      ? plot.x + Math.max(0, Math.min(1, options.playhead)) * plot.w
      : null;
  const splitColumn =
    playheadX != null ? Math.max(0, Math.min(last, columnAtX(plot, envelope.count, playheadX))) : last;

  // 1) inviluppo RMS (più chiaro), porzione riprodotta/non riprodotta
  ctx.save();
  ctx.lineJoin = "round";
  if (playheadX != null) {
    ctx.fillStyle = colors.rmsIdle;
    envelopePath(ctx, envelope, plot, mid, half, splitColumn, last, true);
    ctx.fill();
    ctx.save();
    ctx.beginPath();
    ctx.rect(plot.x, plot.y, Math.max(0, playheadX - plot.x), plot.h);
    ctx.clip();
    ctx.fillStyle = colors.rmsPlayed;
    envelopePath(ctx, envelope, plot, mid, half, 0, splitColumn, true);
    ctx.fill();
    ctx.restore();
  } else {
    ctx.fillStyle = colors.rmsIdle;
    envelopePath(ctx, envelope, plot, mid, half, 0, last, true);
    ctx.fill();
  }
  ctx.restore();

  // 2) poligono picchi specchiato + tratto di bordo 1px
  const drawPeaks = (from: number, to: number, fill: string, edge: string): void => {
    if (to < from) return;
    ctx.save();
    ctx.lineJoin = "round";
    ctx.fillStyle = fill;
    envelopePath(ctx, envelope, plot, mid, half, from, to, false);
    ctx.fill();
    ctx.strokeStyle = edge;
    ctx.lineWidth = 1;
    strokeOutline(ctx, envelope, plot, mid, half, from, to, true);
    strokeOutline(ctx, envelope, plot, mid, half, from, to, false);
    ctx.restore();
  };

  if (playheadX != null) {
    drawPeaks(splitColumn, last, colors.peakIdle, colors.edgeIdle);
    ctx.save();
    ctx.beginPath();
    ctx.rect(plot.x, plot.y, Math.max(0, playheadX - plot.x), plot.h);
    ctx.clip();
    drawPeaks(0, splitColumn, colors.peakPlayed, colors.edgePlayed);
    ctx.restore();
  } else {
    drawPeaks(0, last, colors.peakIdle, colors.edgeIdle);
  }

  // cursore hover
  if (options.hover != null && Number.isFinite(options.hover)) {
    const x = Math.round(plot.x + Math.max(0, Math.min(1, options.hover)) * plot.w) + 0.5;
    ctx.save();
    ctx.strokeStyle = colors.hover;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(x, plot.y);
    ctx.lineTo(x, plot.y + plot.h);
    ctx.stroke();
    ctx.restore();
  }

  // playhead + maniglia
  if (playheadX != null) {
    const x = Math.round(playheadX) + 0.5;
    ctx.strokeStyle = colors.playhead;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, plot.y);
    ctx.lineTo(x, plot.y + plot.h);
    ctx.stroke();
    ctx.fillStyle = colors.handle;
    ctx.fillRect(Math.round(playheadX) - 2, plot.y, 5, 3);
  }
}

/** dBFS stimato dal picco alla posizione indicata (per il tooltip). */
export function peakDbAt(data: WaveformData | null, fraction: number): number | null {
  if (!data?.supported || !data.peaks || data.peaks.min.length === 0) return null;
  const index = Math.min(
    data.peaks.min.length - 1,
    Math.max(0, Math.floor(fraction * data.peaks.min.length)),
  );
  const amplitude = Math.max(
    Math.abs(data.peaks.min[index] ?? 0),
    Math.abs(data.peaks.max[index] ?? 0),
  );
  if (amplitude <= 0.000001) return null;
  return 20 * Math.log10(amplitude);
}

/* ------------------------------------------------------------------ loading */

export function emptyWaveform(reason: string): WaveformData {
  return {
    supported: false,
    reason,
    sampleRate: 0,
    channels: 0,
    bitsPerSample: 0,
    durationSec: 0,
    frames: 0,
    peaks: { min: [], max: [] },
    rms: [],
    source: "none",
  };
}

async function waveformFromWebAudio(
  path: string,
  buckets: number,
): Promise<WaveformData | null> {
  try {
    const { base64, size } = await engineApi.readFileBase64(path);
    if (size > 128 * 1024 * 1024) return null;
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    const buffer = bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength,
    ) as ArrayBuffer;
    const AudioCtor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtor) return null;
    const context = new AudioCtor();
    try {
      const decoded = await context.decodeAudioData(buffer);
      const channels = decoded.numberOfChannels;
      const frames = decoded.length;
      const targetBuckets = Math.max(256, Math.min(buckets, frames));
      const blockSize = Math.max(1, Math.floor(frames / targetBuckets));
      const mins: number[] = [];
      const maxs: number[] = [];
      const rms: number[] = [];
      const data: Float32Array[] = [];
      for (let channel = 0; channel < channels; channel += 1) {
        data.push(decoded.getChannelData(channel));
      }
      for (let bucket = 0; bucket < targetBuckets; bucket += 1) {
        const start = bucket * blockSize;
        const end = Math.min(frames, start + blockSize);
        let min = 1;
        let max = -1;
        let squareSum = 0;
        let count = 0;
        const stride = Math.max(1, Math.floor((end - start) / 1024));
        for (let index = start; index < end; index += stride) {
          let sum = 0;
          for (let channel = 0; channel < channels; channel += 1) {
            sum += data[channel][index] ?? 0;
          }
          const value = sum / Math.max(1, channels);
          if (value < min) min = value;
          if (value > max) max = value;
          squareSum += value * value;
          count += 1;
        }
        if (count === 0) {
          min = 0;
          max = 0;
        }
        mins.push(min);
        maxs.push(max);
        rms.push(count > 0 ? Math.sqrt(squareSum / count) : 0);
      }
      return {
        supported: true,
        reason: null,
        sampleRate: decoded.sampleRate,
        channels,
        bitsPerSample: 0,
        durationSec: decoded.duration,
        frames,
        peaks: { min: mins, max: maxs },
        rms,
        source: "webaudio",
      };
    } finally {
      void context.close();
    }
  } catch {
    return null;
  }
}

/**
 * Sorgente preferita: Web Audio (decodifica reale del file, WAV/MP3/FLAC).
 * Fallback: parser WAV del motore Rust (PCM, anche 24-bit).
 * La fedeltà visiva viene prima del percorso tecnicamente più corto.
 */
export async function loadWaveform(path: string, buckets = 4096): Promise<WaveformData> {
  const fallback = await waveformFromWebAudio(path, buckets).catch(() => null);
  if (fallback) return fallback;
  try {
    const data = await engineApi.readWaveform(path, buckets);
    if (data?.supported) return data;
    return data ?? emptyWaveform("unavailable");
  } catch (error) {
    return emptyWaveform(error instanceof Error ? error.message : "unavailable");
  }
}
