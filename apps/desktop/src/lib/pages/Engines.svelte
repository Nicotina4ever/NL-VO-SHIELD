<script lang="ts">
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
  import { engineApi } from "../ipc";
  import { engineStatusTone } from "../format";
  import type { EngineInfo, InstallHint } from "../types";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";
  import Modal from "../components/Modal.svelte";

  let hintEngine = $state<EngineInfo | null>(null);
  let hint = $state<InstallHint | null>(null);
  let hintLoading = $state(false);
  let hintError = $state<string | null>(null);
  let detailsEngine = $state<EngineInfo | null>(null);

  function detailKey(kind: string, section: string): string | null {
    const known = ["anti_clone", "watermark", "provenance", "fingerprint", "quality"];
    if (!known.includes(kind)) return null;
    return `engines.${section}.${kind}`;
  }

  function statusLabel(status: string): string {
    if (status === "available") return t("engines.statusAvailable");
    if (status === "unavailable") return t("engines.statusUnavailable");
    if (status === "experimental") return t("engines.statusExperimental");
    if (status === "verified") return t("engines.statusVerified");
    return status;
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
    await refreshEngines();
    await loadModels();
  }

  onMount(() => {
    void refreshAll();
  });
</script>

<div class="page">
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
      <div>{engine.error?.message ?? t("dashboard.setupWarningHint")}</div>
    </div>
  {/if}

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
            <div class="small {item.status === 'unavailable' ? 'err-box' : 'warn-box'}">{item.reason}</div>
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
            {#if item.quality_note}
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
            <button class="btn btn--ghost btn--sm" onclick={goToModels}>
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
            <div class="small faint" style="margin-top:4px">{detailsEngine.reason}</div>
          {/if}
        </dd>
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
      {:else if detailsEngine.quality_note}
        <div class="field">
          <span class="field__label">{t("engines.qualityNote")}</span>
          <div class="small">{detailsEngine.quality_note}</div>
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
