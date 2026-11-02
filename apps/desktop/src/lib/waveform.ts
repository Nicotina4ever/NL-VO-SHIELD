/**
 * Forma d'onda: picchi calcolati dal motore Rust (WAV) oppure decodifica
 * Web Audio nel WebView per gli altri formati supportati (es. MP3).
 * Nessun dato esce dal computer.
 */
import { engineApi } from "./ipc";
import type { WaveformData } from "./types";

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
    source: "none",
  };
}

export async function loadWaveform(path: string, buckets = 1600): Promise<WaveformData> {
  try {
    const data = await engineApi.readWaveform(path, buckets);
    if (data?.supported) return data;
    const fallback = await waveformFromWebAudio(path, buckets);
    if (fallback) return fallback;
    return data ?? emptyWaveform("unavailable");
  } catch (err) {
    return emptyWaveform(err instanceof Error ? err.message : "unavailable");
  }
}

function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function waveformFromWebAudio(path: string, buckets: number): Promise<WaveformData | null> {
  try {
    const { base64, size } = await engineApi.readFileBase64(path);
    if (size > 96 * 1024 * 1024) return null;
    const bytes = base64ToBytes(base64);
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
      const targetBuckets = Math.max(64, Math.min(buckets, frames));
      const blockSize = Math.max(1, Math.floor(frames / targetBuckets));
      const mins: number[] = [];
      const maxs: number[] = [];

      const data: Float32Array[] = [];
      for (let ch = 0; ch < channels; ch += 1) data.push(decoded.getChannelData(ch));

      for (let b = 0; b < targetBuckets; b += 1) {
        const start = b * blockSize;
        const end = Math.min(frames, start + blockSize);
        let min = 1;
        let max = -1;
        // campionamento a passo per non scansionare tutto l'audio
        const stride = Math.max(1, Math.floor((end - start) / 512));
        for (let i = start; i < end; i += stride) {
          let sum = 0;
          for (let ch = 0; ch < channels; ch += 1) sum += data[ch][i] ?? 0;
          const value = sum / Math.max(1, channels);
          if (value < min) min = value;
          if (value > max) max = value;
        }
        if (min === 1 && max === -1) {
          min = 0;
          max = 0;
        }
        mins.push(min);
        maxs.push(max);
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
        source: "webaudio",
      };
    } finally {
      void context.close();
    }
  } catch {
    return null;
  }
}

export interface WaveOptions {
  color?: string;
  midline?: string;
  background?: string;
  heightPx?: number;
}

/** Disegna i picchi su un canvas, adattandosi al DPI. */
export function drawWaveform(
  canvas: HTMLCanvasElement,
  waveform: WaveformData | null,
  options: WaveOptions = {},
): void {
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = Math.max(80, canvas.clientWidth || 320);
  const cssHeight = Math.max(48, canvas.clientHeight || options.heightPx || 96);
  if (canvas.width !== Math.round(cssWidth * dpr) || canvas.height !== Math.round(cssHeight * dpr)) {
    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const styles = getComputedStyle(document.documentElement);
  const color =
    options.color ?? (styles.getPropertyValue("--nl-accent").trim() || "#7531fe");
  const midline =
    options.midline ?? (styles.getPropertyValue("--nl-border-strong").trim() || "#423458");

  ctx.fillStyle = options.background ?? "transparent";
  ctx.fillRect(0, 0, cssWidth, cssHeight);

  const mid = cssHeight / 2;
  ctx.strokeStyle = midline;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(cssWidth, mid);
  ctx.stroke();

  const peaks = waveform?.peaks;
  if (!peaks || peaks.min.length === 0) return;

  const count = peaks.min.length;
  const barWidth = cssWidth / count;
  ctx.fillStyle = color;
  const half = mid - 3;
  for (let i = 0; i < count; i += 1) {
    const min = Math.max(-1, Math.min(1, peaks.min[i] ?? 0));
    const max = Math.max(-1, Math.min(1, peaks.max[i] ?? 0));
    const top = mid - max * half;
    const bottom = mid - min * half;
    const height = Math.max(1, bottom - top);
    ctx.fillRect(i * barWidth, top, Math.max(1, barWidth * 0.82), height);
  }
}
