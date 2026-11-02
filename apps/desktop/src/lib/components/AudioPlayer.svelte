<script lang="ts">
  /**
   * Player A/B (originale vs elaborato) per l'ascolto comparato.
   *
   * Implementazione senza `assetProtocol`: i byte dei file scelti dall'utente
   * vengono letti dall'host Rust (`read_file_base64`) e decodificati con Web
   * Audio nel WebView. Nessun autoplay, nessun percorso hardcoded: i percorsi
   * arrivano dal motore / dal selettore file.
   */
  import { onDestroy, onMount } from "svelte";
  import { t } from "../i18n.svelte";
  import { engineApi } from "../ipc";
  import type { AudioTrack, JobStatus, QualityMetrics } from "../types";
  import { formatDuration, formatNumber, jobStatusTone } from "../format";
  import Badge from "./Badge.svelte";
  import Waveform from "./Waveform.svelte";
  import Icon from "./Icon.svelte";

  let {
    tracks = [],
    inputQuality = null,
    outputQuality = null,
    status = null,
  }: {
    tracks?: AudioTrack[];
    inputQuality?: QualityMetrics | null;
    outputQuality?: QualityMetrics | null;
    status?: JobStatus | null;
  } = $props();

  let context: AudioContext | null = null;
  let buffers = $state<Record<string, AudioBuffer>>({});
  let loading = $state<Record<string, boolean>>({});
  let errorText = $state<Record<string, string | null>>({});
  let gains: Record<string, GainNode> = {};
  let sources: Record<string, AudioBufferSourceNode> = {};

  let playing = $state(false);
  let activeIndex = $state(0);
  let sync = $state(true);
  let loop = $state(false);
  let position = $state(0);
  let duration = $state(0);
  let volumes = $state<Record<string, number>>({});
  let mutes = $state<Record<string, boolean>>({});
  let rafId = 0;
  let startedAt = 0;
  let startOffset = 0;

  const activeTrack = $derived(tracks[activeIndex] ?? null);
  const ready = $derived(
    tracks.length > 0 && tracks.every((track) => Boolean(buffers[track.path])),
  );
  const playhead = $derived(duration > 0 ? Math.min(1, Math.max(0, position / duration)) : 0);

  function trackKey(track: AudioTrack): string {
    return track.path;
  }

  function ensureContext(): AudioContext {
    if (!context) {
      const AudioCtor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtor) throw new Error("AudioContext non disponibile");
      context = new AudioCtor();
    }
    return context;
  }

  async function loadTrack(track: AudioTrack): Promise<void> {
    if (buffers[track.path] || loading[track.path]) return;
    loading = { ...loading, [track.path]: true };
    errorText = { ...errorText, [track.path]: null };
    try {
      const { base64, size } = await engineApi.readFileBase64(track.path, 128 * 1024 * 1024);
      if (size <= 0) throw new Error(t("player.decodeError"));
      const binary = atob(base64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      const arrayBuffer = bytes.buffer.slice(
        bytes.byteOffset,
        bytes.byteOffset + bytes.byteLength,
      ) as ArrayBuffer;
      const ctx = ensureContext();
      const buffer = await ctx.decodeAudioData(arrayBuffer);
      buffers = { ...buffers, [track.path]: buffer };
      if (duration === 0) duration = buffer.duration;
      const gain = ctx.createGain();
      gain.gain.value = volumes[track.path] ?? 1;
      gain.connect(ctx.destination);
      gains = { ...gains, [track.path]: gain };
    } catch (error) {
      errorText = {
        ...errorText,
        [track.path]:
          error instanceof Error ? `${t("player.decodeError")} (${error.message})` : t("player.decodeError"),
      };
    } finally {
      loading = { ...loading, [track.path]: false };
    }
  }

  function stopSources(): void {
    for (const key of Object.keys(sources)) {
      try {
        sources[key].onended = null;
        sources[key].stop();
      } catch {
        /* già fermo */
      }
    }
    sources = {};
  }

  function tick(): void {
    if (!context || !playing) return;
    position = Math.min(duration, startOffset + context.currentTime - startedAt);
    if (position >= duration) {
      if (loop) {
        playFrom(0);
      } else {
        pause();
        position = duration;
      }
      return;
    }
    rafId = requestAnimationFrame(tick);
  }

  function playFrom(offset: number): void {
    if (!ready || !context) return;
    stopSources();
    void context.resume();
    const start = Math.max(0, Math.min(duration, offset));
    for (const track of tracks) {
      const buffer = buffers[track.path];
      if (!buffer) continue;
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(gains[track.path] ?? context.destination);
      source.loop = false;
      source.start(0, Math.min(start, buffer.duration));
      sources[track.path] = source;
    }
    startedAt = context.currentTime;
    startOffset = start;
    position = start;
    playing = true;
    cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(tick);
  }

  async function play(): Promise<void> {
    if (!ready) return;
    if (!context) return;
    if (position >= duration) position = 0;
    playFrom(position);
  }

  function pause(): void {
    if (!context) return;
    const current = Math.min(duration, startOffset + (context.currentTime - startedAt));
    stopSources();
    playing = false;
    cancelAnimationFrame(rafId);
    position = current;
  }

  function stop(): void {
    stopSources();
    playing = false;
    cancelAnimationFrame(rafId);
    position = 0;
  }

  function togglePlay(): void {
    if (playing) pause();
    else void play();
  }

  function seekTo(fraction: number): void {
    if (!ready) return;
    const target = Math.max(0, Math.min(1, fraction)) * duration;
    if (playing) playFrom(target);
    else position = target;
  }

  function switchTrack(index: number): void {
    const previous = position;
    activeIndex = Math.max(0, Math.min(tracks.length - 1, index));
    if (playing) {
      playFrom(sync ? previous : 0);
    } else if (sync) {
      position = previous;
    } else {
      position = 0;
    }
  }

  function setVolume(key: string, value: number): void {
    volumes = { ...volumes, [key]: value };
    const gain = gains[key];
    if (gain) gain.gain.value = mutes[key] ? 0 : value;
  }

  function toggleMute(key: string): void {
    const next = !mutes[key];
    mutes = { ...mutes, [key]: next };
    const gain = gains[key];
    if (gain) gain.gain.value = next ? 0 : (volumes[key] ?? 1);
  }

  function metricRow(label: string, input: unknown, output: unknown, suffix = ""): [string, string, string] | null {
    const read = (value: unknown): string => {
      if (value == null) return "—";
      if (typeof value === "number") return `${formatNumber(value, 2)}${suffix}`;
      if (typeof value === "boolean") return value ? t("common.yes") : t("common.no");
      if (typeof value === "string") return value;
      return "—";
    };
    if (input == null && output == null) return null;
    return [label, read(input), read(output)];
  }

  const metricRows = $derived.by(() => {
    const rows = [
      metricRow(t("player.lufs"), inputQuality?.lufs ?? inputQuality?.lufs_integrated, outputQuality?.lufs ?? outputQuality?.lufs_integrated, " LUFS"),
      metricRow(t("player.peak"), inputQuality?.peak_dbfs, outputQuality?.peak_dbfs, " dBFS"),
      metricRow(t("player.truePeak"), inputQuality?.true_peak_dbfs, outputQuality?.true_peak_dbfs, " dBFS"),
      metricRow(t("player.clipping"), inputQuality?.clipping_samples, outputQuality?.clipping_samples),
      metricRow(t("player.snr"), inputQuality?.snr_db, outputQuality?.snr_db, " dB"),
      metricRow(t("player.sampleRate"), inputQuality?.sample_rate, outputQuality?.sample_rate, " Hz"),
    ];
    return rows.filter((row): row is [string, string, string] => row !== null);
  });

  onMount(() => {
    for (const track of tracks) {
      volumes[track.path] = volumes[track.path] ?? 1;
      mutes[track.path] = mutes[track.path] ?? false;
      void loadTrack(track);
    }
  });

  // Carica nuove tracce quando il job produce un nuovo output.
  $effect(() => {
    for (const track of tracks) {
      if (!buffers[track.path] && !loading[track.path]) void loadTrack(track);
    }
  });

  onDestroy(() => {
    stopSources();
    cancelAnimationFrame(rafId);
    if (context) void context.close();
  });
</script>

<div class="col" style="gap:12px">
  <div class="row">
    <span class="field__label">{t("player.title")}</span>
    {#if status}
      <Badge label={t(`status.${status}`)} tone={jobStatusTone(status)} />
    {/if}
    <span class="spacer"></span>
    <span class="small faint mono">
      {formatDuration(position)} / {formatDuration(duration)}
    </span>
  </div>

  {#if tracks.length === 0}
    <div class="empty">{t("player.noTracks")}</div>
  {:else}
    <div class="seg" role="group" aria-label={t("player.title")}>
      {#each tracks as track, index (track.path)}
        <button
          class="seg__item {activeIndex === index ? 'active' : ''}"
          onclick={() => switchTrack(index)}
          aria-pressed={activeIndex === index}
          title={track.path}
        >
          {track.label}
        </button>
      {/each}
    </div>

    {#if activeTrack}
      <Waveform
        path={activeTrack.path}
        tall
        interactive
        playhead={playhead}
        onseek={(fraction) => seekTo(fraction)}
        label={activeTrack.label}
      />
    {/if}

    <div class="row gap-sm">
      <button class="btn btn--primary btn--sm" onclick={togglePlay} disabled={!ready} aria-label={t("player.playPause")}>
        <Icon name={playing ? "stop" : "play"} size={14} />
        {playing ? t("player.pause") : t("player.play")}
      </button>
      <button class="btn btn--sm" onclick={stop} disabled={!ready} aria-label={t("player.stop")}>
        <Icon name="stop" size={13} />
        {t("player.stop")}
      </button>
      {#each tracks as track (track.path)}
        <button
          class="btn btn--ghost btn--sm"
          onclick={() => toggleMute(trackKey(track))}
          aria-pressed={mutes[trackKey(track)] ?? false}
          title={track.label}
        >
          {mutes[trackKey(track)] ? "🔇" : "🔊"} {track.label}
        </button>
      {/each}
      <span class="spacer"></span>
      <label class="check" style="font-size:12px">
        <input type="checkbox" bind:checked={sync} />
        <span>{t("player.sync")}</span>
      </label>
      <label class="check" style="font-size:12px">
        <input type="checkbox" bind:checked={loop} />
        <span>{t("player.loop")}</span>
      </label>
    </div>

    {#if activeTrack}
      <div class="row gap-sm">
        <span class="small faint">{t("player.volume")}</span>
        <input
          type="range"
          min="0"
          max="1"
          step="0.01"
          value={volumes[activeTrack.path] ?? 1}
          oninput={(event) => setVolume(activeTrack.path, Number(event.currentTarget.value))}
          style="max-width:200px"
          aria-label={t("player.volume")}
        />
      </div>
    {/if}

    {#if tracks.length > 1}
      <div class="btn-row">
        <button class="btn btn--sm" onclick={() => switchTrack(0)} aria-pressed={activeIndex === 0}>
          {t("player.listenInput")}
        </button>
        <button class="btn btn--sm" onclick={() => switchTrack(1)} aria-pressed={activeIndex === 1}>
          {t("player.listenOutput")}
        </button>
      </div>
    {/if}

    {#each tracks as track (track.path)}
      {#if loading[track.path]}
        <div class="row gap-sm small muted"><span class="spinner"></span>{t("player.loading")}</div>
      {/if}
      {#if errorText[track.path]}
        <div class="err-box small">{errorText[track.path]}</div>
      {/if}
    {/each}
  {/if}

  {#if metricRows.length > 0}
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t("player.metric")}</th>
            <th>{t("player.input")}</th>
            <th>{t("player.output")}</th>
          </tr>
        </thead>
        <tbody>
          {#each metricRows as [label, input, output]}
            <tr>
              <td class="muted small">{label}</td>
              <td class="mono small">{input}</td>
              <td class="mono small">{output}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  <div class="small faint">{t("player.note")}</div>
</div>
