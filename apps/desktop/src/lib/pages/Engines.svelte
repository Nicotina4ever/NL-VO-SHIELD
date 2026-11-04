<script lang="ts">
  import { ui, toast } from "../ui.svelte";
  import { onMount } from "svelte";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import { goToModels } from "../ui.svelte";
  import {
    engine,
    refreshEngines,
    loadModels,
    modelsForEngine,
    engineWeightsState,
  } from "../engine.svelte";
  import { engineApi, aiApi, componentsApi, type AiRuntimeStatus, type ComponentInfo } from "../ipc";
  import { engineStatusTone, formatBytes } from "../format";
  import type { EngineInfo, InstallHint, ModelInfo } from "../types";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";
  import Modal from "../components/Modal.svelte";

  let hintEngine = $state<EngineInfo | null>(null);
  let hint = $state<InstallHint | null>(null);
  let hintLoading = $state(false);
  let hintError = $state<string | null>(null);
  let detailsEngine = $state<EngineInfo | null>(null);
  let installing = $state<string | null>(null);
  let components = $state<Record<string, ComponentInfo>>({});
  let componentError = $state<string | null>(null);
  const BOOTSTRAP_IDS = new Set(["safespeech", "mimic_blocker", "enkidu", "e2e_vguard"]);

  async function loadComponents(): Promise<void> {
    try {
      const res = await componentsApi.list();
      components = Object.fromEntries(res.items.map((item) => [item.id, item]));
      componentError = null;
    } catch (err) { componentError = hostErrorMessage(err as { code?: string; message?: string }); }
  }

  function engineComponentState(item: { id: string; status: string }): string {
    const comp = components[item.id];
    if (!comp) return item.status;
    if (comp.builtin) return comp.installed ? (item.status === "experimental" ? "experimental" : "available") : "unavailable";
    if (comp.installed && comp.runtime_present) return "available";
    if (comp.installed) return "repair";
    if (comp.bootstrap) return "install";
    return "unavailable";
  }

  function componentActionLabel(state: string): string {
    if (state === "install") return t("components.install");
    if (state === "repair") return t("components.verify");
    return t("engines.installHint");
  }

  async function componentAction(item: EngineInfo): Promise<void> {
    const state = engineComponentState(item);
    if (state === "install" || state === "repair") {
      await installEngine(item.id);
    } else {
      await openHint(item);
    }
  }

  async function installEngine(id: string): Promise<void> {
    installing = id;
    try {
      await componentsApi.action({ id, action: "install", confirmed: true });
      await refreshEngines();
      await loadComponents();
      toast("ok", t("components.install"), id);
    } catch (err) {
      toast("err", t("components.install"), hostErrorMessage(err as { code?: string; message?: string }));
    } finally {
      installing = null;
    }
  }

  let aiRuntime = $state<AiRuntimeStatus | null>(null);
  let aiError = $state<string | null>(null);

  /** Motori censiti dall'advisor: per questi esistono testi IT/EN dedicati. */
  const ADVISED_ENGINES = [
    "audioseal",
    "c2pa",
    "fingerprint",
    "quality",
    "safespeech",
    "spectral_cloak",
    "watermark_dsp",
  ];
  const KNOWN_KINDS = ["anti_clone", "watermark", "provenance", "fingerprint", "quality"];
  /** Soglia indicativa: sotto questa VRAM la variante GPU resta possibile ma sconsigliata. */
  const VRAM_COMFORT_BYTES = 4 * 1024 * 1024 * 1024;

  interface Recommendation {
    id: string;
    name: string;
    kind: string;
    status: string;
    availability: string;
    mode: string;
    requirements: string;
    install: string;
    quality: string;
    efficacy: string;
    fallback: string;
    advice: string;
    models: ModelInfo[];
    weights: "present" | "missing" | "manual" | "unknown";
    advised: boolean;
  }

  function detailKey(kind: string, section: string): string | null {
    if (!KNOWN_KINDS.includes(kind)) return null;
    return `engines.${section}.${kind}`;
  }

  function statusLabel(status: string): string {
    if (status === "available") return t("engines.statusAvailable");
    if (status === "unavailable") return t("engines.statusUnavailable");
    if (status === "experimental") return t("engines.statusExperimental");
    if (status === "verified") return t("engines.statusVerified");
    return t("engines.statusError");
  }

  function kindLabel(kind: string): string {
    const map: Record<string, string> = {
      anti_clone: "engines.kindAntiClone",
      watermark: "engines.kindWatermark",
      provenance: "engines.kindProvenance",
      fingerprint: "engines.kindFingerprint",
      quality: "engines.kindQuality",
    };
    const key = map[kind];
    return key ? t(key) : kind;
  }

  function weightsLabel(engineId: string): string {
    const state = engineWeightsState(engineId);
    if (state === "present") return t("engines.weightsPresent");
    if (state === "missing") return t("engines.weightsMissing");
    if (state === "manual") return t("engines.weightsManual");
    return t("common.unknown");
  }

  /* ----------------------------- hardware ------------------------------- */

  const hardware = $derived(engine.host?.hardware ?? null);

  function aiVariantLabel(variant: string | null | undefined): string {
    if (variant === "gpu" || variant === "cuda") return t("settings.aiVariantGpu");
    if (variant === "cpu") return t("settings.aiVariantCpu");
    return variant ? variant : t("engines.hwUnknown");
  }

  /** `nvidia-smi` del motore restituisce "Nome, 8188 MiB": usato solo se il Rust non ha dati. */
  function gpuFromAiRuntime(): { name: string | null; vramBytes: number | null } {
    const raw = aiRuntime?.gpu?.name ?? null;
    if (!raw) return { name: null, vramBytes: null };
    const parts = raw.split(",");
    const name = parts[0]?.trim() || null;
    const match = raw.match(/(\d+(?:\.\d+)?)\s*(MiB|GiB|MB|GB)/i);
    if (!match) return { name, vramBytes: null };
    const value = Number(match[1]);
    if (!Number.isFinite(value)) return { name, vramBytes: null };
    const unit = match[2].toLowerCase();
    const factor = unit.startsWith("g") ? 1024 ** 3 : 1024 ** 2;
    return { name, vramBytes: Math.round(value * factor) };
  }

  const gpuName = $derived(hardware?.gpuName ?? gpuFromAiRuntime().name);
  const gpuVram = $derived(hardware?.gpuVramBytes ?? gpuFromAiRuntime().vramBytes);

  /**
   * Solo una GPU NVIDIA/CUDA può eseguire la variante GPU del Runtime AI:
   * una GPU Intel/AMD non abilita CUDA, quindi non va promessa come tale.
   */
  const cudaGpuName = $derived.by(() => {
    if (aiRuntime?.gpu?.available) return aiRuntime.gpu.name?.split(",")[0]?.trim() ?? gpuName;
    const hw = hardware;
    if (!hw) return null;
    if (hw.gpuSource === "nvidia-smi") return hw.gpuName ?? null;
    if ((hw.gpuVendor ?? "").toUpperCase() === "NVIDIA") return hw.gpuName ?? null;
    return (hw.gpuName ?? "").toLowerCase().includes("nvidia") ? hw.gpuName : null;
  });
  const cudaGpuVram = $derived(cudaGpuName ? gpuVram : null);

  const cpuText = $derived(hardware?.cpuModel ?? t("engines.hwUnknown"));
  const cpuCoresText = $derived(
    hardware?.cpuLogicalCores
      ? t("engines.hwCores", { n: hardware.cpuLogicalCores })
      : t("engines.hwUnknown"),
  );
  const ramText = $derived(
    hardware?.ramTotalBytes ? formatBytes(hardware.ramTotalBytes) : t("engines.hwUnknown"),
  );
  const gpuText = $derived(gpuName ?? t("engines.hwNoGpu"));
  const vramText = $derived(
    gpuVram ? formatBytes(gpuVram) : gpuName ? t("engines.hwNoVram") : t("engines.hwCpuOnly"),
  );
  const osText = $derived(engine.host ? `${engine.host.os} · ${engine.host.arch}` : t("engines.hwUnknown"));
  const aiRuntimeText = $derived.by(() => {
    if (aiError) return t("engines.hwAiUnavailable");
    if (!aiRuntime) return t("common.loading");
    if (!aiRuntime.installed) return t("engines.hwAiNotInstalled");
    return t("engines.hwAiInstalled", { variant: aiVariantLabel(aiRuntime.variant ?? aiRuntime.device) });
  });

  /* -------------------------- recommendations --------------------------- */

  function recText(engineId: string, field: string): string | null {
    if (!ADVISED_ENGINES.includes(engineId)) return null;
    const key = `engines.rec.${engineId}.${field}`;
    const value = t(key);
    return value === key ? null : value;
  }

  function kindRecText(section: "modeByKind" | "efficacyByKind", kind: string): string | null {
    if (!KNOWN_KINDS.includes(kind)) return null;
    const key = `engines.rec.${section}.${kind}`;
    const value = t(key);
    return value === key ? null : value;
  }

  function availabilityText(item: EngineInfo): string {
    if (item.status === "verified") return t("engines.rec.statusVerifiedNow");
    if (item.status === "available") return t("engines.rec.statusAvailableNow");
    if (item.status === "experimental") return t("engines.rec.statusExperimentalNow");
    if (item.status === "unavailable") return t("engines.rec.statusUnavailableNow");
    return statusLabel(item.status);
  }

  function installText(item: EngineInfo): string {
    const models = modelsForEngine(item.id);
    const present = models.filter((model) => model.present).length;
    const state = engineWeightsState(item.id);
    if (state === "manual") return t("engines.rec.installManual");
    const total = models.length;
    if (state === "present") return t("engines.rec.installPresent", { n: present, total });
    return t("engines.rec.installMissing", { n: present, total });
  }

  /**
   * Fallback dichiarato: un motore non viene mai escluso per VRAM.
   * AudioSeal è l'unico con variante GPU realmente selezionabile.
   */
  function fallbackText(item: EngineInfo): string {
    if (item.id === "audioseal") {
      if (!cudaGpuName) return t("engines.rec.fallbackNoGpu");
      const vram = cudaGpuVram ? formatBytes(cudaGpuVram) : t("engines.hwNoVram");
      if (cudaGpuVram && cudaGpuVram < VRAM_COMFORT_BYTES) {
        return t("engines.rec.fallbackLowVram", { vram });
      }
      return t("engines.rec.fallbackGpu", { name: cudaGpuName, vram });
    }
    if (item.status === "unavailable") return t("engines.rec.fallbackNone");
    return t("engines.rec.fallbackCpu");
  }

  function recommendationFor(item: EngineInfo): Recommendation {
    const advised = ADVISED_ENGINES.includes(item.id);
    const quality = recText(item.id, "quality") ?? item.quality_note ?? t("common.unknown");
    const efficacy =
      recText(item.id, "efficacy") ??
      kindRecText("efficacyByKind", item.kind) ??
      t("engines.rec.notMeasured");
    const mode =
      recText(item.id, "mode") ?? kindRecText("modeByKind", item.kind) ?? t("common.unknown");
    const requirements =
      recText(item.id, "requires") ??
      (item.requires.length > 0 ? item.requires.join(", ") : t("common.unknown"));
    const advice = recText(item.id, "advice") ?? t("engines.rec.unknownEngine");
    return {
      id: item.id,
      name: item.name,
      kind: item.kind,
      status: item.status,
      availability: availabilityText(item),
      mode,
      requirements,
      install: installText(item),
      quality,
      efficacy,
      fallback: fallbackText(item),
      advice,
      models: modelsForEngine(item.id),
      weights: engineWeightsState(item.id),
      advised,
    };
  }

  const recommendations = $derived.by(() => engine.engines.map(recommendationFor));

  /* ------------------------------ actions ------------------------------- */

  async function loadAi(): Promise<void> {
    aiError = null;
    try {
      const response = await aiApi.status();
      aiRuntime = response.runtime;
    } catch (err) {
      aiError = hostErrorMessage(err as { code?: string; message?: string });
    }
  }

  async function openHint(item: EngineInfo): Promise<void> {
    hintEngine = item;
    hint = null;
    hintError = null;
    hintLoading = true;
    try {
      hint = await engineApi.installHint(item.id);
    } catch (err) {
      hintError = hostErrorMessage(err as { code?: string; message?: string });
    } finally {
      hintLoading = false;
    }
  }

  async function refreshAll(): Promise<void> {
    await Promise.allSettled([refreshEngines(), loadModels()]);
    await loadAi();
  }

  onMount(() => {
    void loadComponents();
    void refreshAll();
  });
</script>

<div class="page mode-{ui.viewMode}">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("engines.title")}</h1>
      <p>{t("engines.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn btn--sm" onclick={() => void refreshAll()} disabled={engine.enginesLoading}>
        {#if engine.enginesLoading}<span class="spinner"></span>{/if}
        <Icon name="refresh" size={14} />
        {t("engines.refreshList")}
      </button>
      <button class="btn btn--sm btn--ghost" onclick={goToModels}>
        <Icon name="download" size={14} />
        {t("engines.manageModels")}
      </button>
    </div>
  </div>

  {#if !engine.online}
    <div class="warn-box">
      <div class="warn-box__title">{t("dashboard.setupWarning")}</div>
      <div>{hostErrorMessage(engine.error) || t("dashboard.setupWarningHint")}</div>
    </div>
  {/if}

  <div class="card">
    <div class="card__head">
      <h2>{t("engines.hwTitle")}</h2>
      <span class="spacer"></span>
      <Badge label={t("engines.hwBadge")} tone="muted" />
    </div>
    <div class="card__body">
      <p class="small muted" style="margin:0 0 10px">{t("engines.hwSubtitle")}</p>
      <div class="hw-grid">
        <div class="hw-item">
          <span class="hw-item__label">{t("engines.hwCpu")}</span>
          <span class="hw-item__value">{cpuText}</span>
          <span class="small faint">{cpuCoresText}</span>
        </div>
        <div class="hw-item">
          <span class="hw-item__label">{t("engines.hwRam")}</span>
          <span class="hw-item__value">{ramText}</span>
        </div>
        <div class="hw-item">
          <span class="hw-item__label">{t("engines.hwGpu")}</span>
          <span class="hw-item__value">{gpuText}</span>
        </div>
        <div class="hw-item">
          <span class="hw-item__label">{t("engines.hwVram")}</span>
          <span class="hw-item__value">{vramText}</span>
        </div>
        <div class="hw-item">
          <span class="hw-item__label">{t("engines.hwAiRuntime")}</span>
          <span class="hw-item__value">{aiRuntimeText}</span>
        </div>
        <div class="hw-item">
          <span class="hw-item__label">{t("engines.hwOs")}</span>
          <span class="hw-item__value">{osText}</span>
        </div>
      </div>
    </div>
  </div>

  {#if componentError}<div class="warn-box small">{componentError}</div>{/if}
  <div class="card">
    <div class="card__head">
      <h2>{t("engines.rec.title")}</h2>
    </div>
    <div class="card__body">
      <p class="small muted" style="margin:0 0 10px">{t("engines.rec.subtitle")}</p>
      {#if recommendations.length === 0}
        <div class="empty">{engine.enginesLoading ? t("common.loading") : t("engines.noEngines")}</div>
      {:else}
        <div class="rec-grid">
          {#each recommendations as rec (rec.id)}
            <article class="rec-card {rec.status === 'unavailable' ? 'rec-card--off' : ''}">
              <div class="rec-card__head">
                <div class="stack" style="gap:2px">
                  <span class="engine-card__title">{rec.name}</span>
                  <span class="engine-card__sub">{kindLabel(rec.kind)} · {rec.id}</span>
                </div>
                <Badge label={statusLabel(engineComponentState(rec))} tone={engineStatusTone(engineComponentState(rec))} />
              </div>

              <dl class="kv">
                <dt>{t("common.status")}</dt>
                <dd>{rec.availability}</dd>
                <dt>{t("engines.rec.mode")}</dt>
                <dd>{rec.mode}</dd>
                <dt>{t("engines.rec.requirements")}</dt>
                <dd>{rec.requirements}</dd>
                <dt>{t("engines.rec.install")}</dt>
                <dd>{BOOTSTRAP_IDS.has(rec.id) ? (components[rec.id]?.installed ? t("engines.rec.installedVerified") : t("engines.rec.installAutomatic")) : rec.install}</dd>
                <dt>{t("engines.rec.quality")}</dt>
                <dd>{rec.quality}</dd>
                <dt>{t("engines.rec.efficacy")}</dt>
                <dd>{rec.efficacy}</dd>
              </dl>

              <div class="hint-box small">
                <strong>{t("engines.rec.fallback")}:</strong> {rec.fallback}
              </div>
              <div class="small">
                <strong>{t("engines.rec.advice")}:</strong> {rec.advice}
              </div>

              <div class="rec-card__foot">
                <button class="btn btn--sm" onclick={() => (detailsEngine = engine.engines.find((item) => item.id === rec.id) ?? null)}>
                  <Icon name="info" size={14} />
                  {t("engines.details")}
                </button>
                <button
                  class="btn btn--ghost btn--sm"
                  onclick={() => {
                    const found = engine.engines.find((item) => item.id === rec.id);
                    if (found) void openHint(found);
                  }}
                >
                  {t("engines.installHint")}
                </button>
                {#if BOOTSTRAP_IDS.has(rec.id)}
                  <button class="btn btn--primary btn--sm" disabled={installing !== null} onclick={() => { const found = engine.engines.find((e) => e.id === rec.id); if (found) void componentAction(found); }}>
                    {installing === rec.id ? t("common.running") : componentActionLabel(engineComponentState(rec))}
                  </button>
                {/if}
                <button class="btn btn--ghost btn--sm" onclick={goToModels}>
                  {t("engines.manageModels")}
                </button>
              </div>
            </article>
          {/each}
        </div>
        <div class="hint-box small" style="margin-top:10px">{t("engines.rec.noEffectiveness")}</div>
      {/if}
    </div>
  </div>

  {#if engine.engines.length === 0}
    <div class="card">
      <div class="card__body">
        <div class="empty">{engine.enginesLoading ? t("common.loading") : t("engines.noEngines")}</div>
      </div>
    </div>
  {:else}
    <div class="engine-grid">
      {#each engine.engines as item (item.id)}
        {@const models = modelsForEngine(item.id)}
        {@const weights = engineWeightsState(item.id)}
        <div class="engine-card {item.status === 'unavailable' ? 'engine-card--off' : ''}">
          <div class="engine-card__head">
            <div class="stack" style="gap:2px">
              <span class="engine-card__title">{item.name}</span>
              <span class="engine-card__sub">{item.id} · {t("engines.versionLine", { v: item.version })}</span>
            </div>
            <Badge label={statusLabel(item.status)} tone={engineStatusTone(item.status)} />
          </div>

          <div class="row gap-sm">
            <span class="chip">{kindLabel(item.kind)}</span>
            {#if item.requires_download}
              <span class="chip">{t("engines.requiresDownload")}</span>
            {/if}
          </div>

          {#if item.reason}
            <details>
              <summary class="small faint" style="cursor:pointer">{t("engines.reasonFromEngine")}</summary>
              <div class="small {item.status === 'unavailable' ? 'err-box' : 'warn-box'}" style="margin-top:4px">
                {item.reason}
              </div>
            </details>
          {/if}

          <dl class="kv small">
            <dt>{t("engines.capabilities")}</dt>
            <dd>
              <div class="chip-list">
                {#each item.capabilities ?? [] as capability}
                  <span class="chip">{capability}</span>
                {:else}
                  <span class="faint">—</span>
                {/each}
              </div>
            </dd>
            <dt>{t("engines.supportedOs")}</dt>
            <dd class="mono small">{(item.supported_os ?? []).join(", ") || "—"}</dd>
            <dt>{t("engines.requires")}</dt>
            <dd class="mono small">{(item.requires ?? []).join(", ") || "—"}</dd>
            <dt>{t("engines.weightsPresent")}</dt>
            <dd>
              <span class="pill-bool">
                <span
                  class="dot {weights === 'present' ? 'dot--ok' : weights === 'missing' ? 'dot--err' : ''}"
                ></span>
                {weightsLabel(item.id)}
                {#if models.length > 0}
                  <span class="faint small">({models.filter((model) => model.present).length}/{models.length})</span>
                {/if}
              </span>
            </dd>
            {#if item.path}
              <dt>{t("engines.enginePath")}</dt>
              <dd class="mono small break-all">{item.path}</dd>
            {/if}
            {#if ADVISED_ENGINES.includes(item.id)}
              <dt>{t("engines.rec.quality")}</dt>
              <dd class="small">{recText(item.id, "quality")}</dd>
            {:else if item.quality_note}
              <dt>{t("engines.qualityNote")}</dt>
              <dd class="small">{item.quality_note}</dd>
            {/if}
          </dl>

          <div class="btn-row" style="margin-top:auto">
            <button class="btn btn--sm" onclick={() => (detailsEngine = item)}>
              <Icon name="info" size={14} />
              {t("engines.details")}
            </button>
            <button class="btn btn--ghost btn--sm" onclick={() => void openHint(item)}>
              {t("engines.installHint")}
            </button>
            <button
              class="btn btn--sm {item.status === 'unavailable' ? 'btn--primary' : 'btn--ghost'}"
              onclick={goToModels}
            >
              {t("engines.manageModels")}
            </button>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <div class="hint-box small">
    {t("engines.downloadManual")} · <button class="btn btn--ghost btn--sm" onclick={goToModels}>
      {t("engines.gotoModels")}
    </button>
  </div>
</div>

{#if detailsEngine}
  {@const detailModels = modelsForEngine(detailsEngine.id)}
  {@const detailQuality = recText(detailsEngine.id, "quality")}
  {@const detailEfficacy =
    recText(detailsEngine.id, "efficacy") ??
    kindRecText("efficacyByKind", detailsEngine.kind) ??
    t("engines.rec.notMeasured")}
  {@const detailMode =
    recText(detailsEngine.id, "mode") ??
    kindRecText("modeByKind", detailsEngine.kind) ??
    t("common.unknown")}
  <Modal title={`${t("engines.detailsTitle")} — ${detailsEngine.name}`} onclose={() => (detailsEngine = null)} wide>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (detailsEngine = null)}>{t("common.close")}</button>
      {#if detailsEngine?.status === "unavailable"}
        <button class="btn btn--primary" onclick={goToModels}>{t("engines.manageModels")}</button>
      {/if}
    {/snippet}
    <div class="col" style="gap:12px">
      <dl class="kv">
        <dt>{t("common.name")}</dt>
        <dd class="mono">{detailsEngine.name}</dd>
        <dt>{t("engines.kind")}</dt>
        <dd>{kindLabel(detailsEngine.kind)}</dd>
        <dt>{t("common.version")}</dt>
        <dd class="mono">{detailsEngine.version}</dd>
        <dt>{t("common.status")}</dt>
        <dd>
          <Badge label={statusLabel(detailsEngine.status)} tone={engineStatusTone(detailsEngine.status)} />
          {#if detailsEngine.reason}
            <div class="small faint" style="margin-top:4px">
              {t("engines.reasonFromEngine")}: {detailsEngine.reason}
            </div>
          {/if}
        </dd>
        <dt>{t("engines.rec.mode")}</dt>
        <dd>{detailMode}</dd>
        <dt>{t("engines.rec.efficacy")}</dt>
        <dd>{detailEfficacy}</dd>
      </dl>

      {#if detailKey(detailsEngine.kind, "purpose")}
        <div class="field">
          <span class="field__label">{t("engines.purpose")}</span>
          <div class="small">{t(detailKey(detailsEngine.kind, "purpose") as string)}</div>
        </div>
        <div class="field">
          <span class="field__label">{t("engines.audioEffect")}</span>
          <div class="small">{t(detailKey(detailsEngine.kind, "audioEffect") as string)}</div>
        </div>
        <div class="field">
          <span class="field__label">{t("engines.prerequisites")}</span>
          <div class="small">{t(detailKey(detailsEngine.kind, "prerequisites") as string)}</div>
          <div class="small faint mono">
            {(detailsEngine.requires ?? []).join(", ") || "—"}
          </div>
        </div>
        <div class="field">
          <span class="field__label">{t("engines.limits")}</span>
          <div class="small">{t(detailKey(detailsEngine.kind, "limits") as string)}</div>
        </div>
        <div class="field">
          <span class="field__label">{t("engines.whenToUse")}</span>
          <div class="small">{t(detailKey(detailsEngine.kind, "whenToUse") as string)}</div>
        </div>
      {:else if detailQuality}
        <div class="field">
          <span class="field__label">{t("engines.rec.quality")}</span>
          <div class="small">{detailQuality}</div>
        </div>
      {/if}

      {#if detailModels.length > 0}
        <div class="field">
          <span class="field__label">{t("engines.weightsPresent")}</span>
          <div class="col" style="gap:3px">
            {#each detailModels as model (model.id)}
              <div class="row gap-sm small">
                <span class="dot {model.present ? 'dot--ok' : 'dot--err'}"></span>
                <span class="mono">{model.id}</span>
                <span class="faint">{model.present ? t("settings.modelPresent") : t("settings.modelMissing")}</span>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      {#if detailsEngine.status === "unavailable"}
        <div class="err-box small">{t("engines.notAvailableHint")} {t("engines.downloadManual")}</div>
      {/if}
    </div>
  </Modal>
{/if}

{#if hintEngine}
  <Modal
    title={`${t("engines.installHintTitle")} — ${hintEngine.name}`}
    onclose={() => {
      hintEngine = null;
      hint = null;
      hintError = null;
    }}
  >
    <div class="col" style="gap:10px">
      {#if hintLoading}
        <div class="row"><span class="spinner"></span><span class="muted">{t("common.loading")}</span></div>
      {:else if hintError}
        <div class="err-box">
          <div class="err-box__title">{t("common.error")}</div>
          <div>{hintError}</div>
        </div>
      {:else if hint}
        <dl class="kv">
          <dt>{t("engines.requiresDownload")}</dt>
          <dd>{hint.requires_download ? t("common.yes") : t("common.no")}</dd>
        </dl>
        <div class="field">
          <span class="field__label">{t("engines.installInstructions")}</span>
          <pre class="mono small" style="white-space:pre-wrap;overflow:auto;max-height:320px">
{hint.instructions}</pre>
        </div>
        <div class="hint-box small">{t("engines.downloadManual")}</div>
      {/if}
    </div>
  </Modal>
{/if}
