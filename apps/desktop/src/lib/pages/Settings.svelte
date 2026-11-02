<script lang="ts">
  import { onMount } from "svelte";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import {
    engine,
    loadHost,
    loadModelSettings,
    loadModels,
    loadSettings,
    restartEngine,
    connect,
    saveSettings,
    checkUpdates,
    setModelDownloading,
  } from "../engine.svelte";
  import { modelsApi, engineApi } from "../ipc";
  import { ui, toast, setLanguage as setUiLanguage, setTheme as setUiTheme } from "../ui.svelte";
  import { i18n } from "../i18n.svelte";
  import { pickDirectory } from "../dialogs";
  import { formatBytes, formatDate } from "../format";
  import type { ModelInfo } from "../types";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";
  import Modal from "../components/Modal.svelte";
  import ProgressBar from "../components/ProgressBar.svelte";

  let outputDir = $state("");
  let openAfterJob = $state(false);
  let autoUpdateCheck = $state(false);
  let autoPrompt = $state(true);
  let engineJson = $state("{}");
  let engineJsonDirty = $state(false);
  let saving = $state(false);

  let selectedModels = $state<string[]>([]);
  let deleting = $state(false);
  let clearingCache = $state(false);
  let checkingUpdates = $state(false);
  let pendingClearCache = $state(false);
  let pendingDelete = $state(false);

  const host = $derived(engine.host);
  const modelSettings = $derived(engine.modelSettings);

  function syncFromSettings(): void {
    const hostSettings = engine.settings?.host;
    outputDir = hostSettings?.outputDir ?? host?.paths.outputDir ?? "";
    openAfterJob = Boolean(hostSettings?.openAfterJob);
    autoUpdateCheck = Boolean(hostSettings?.autoUpdateCheck);
    const rawEngine = engine.settings?.settings;
    if (!engineJsonDirty) {
      engineJson = JSON.stringify(rawEngine ?? {}, null, 2);
    }
  }

  function hostPayload(): Record<string, unknown> {
    return {
      language: i18n.lang,
      theme: ui.theme,
      outputDir: outputDir || null,
      openAfterJob,
      autoUpdateCheck,
    };
  }

  function integrityTone(value: string | null | undefined): "ok" | "warn" | "err" | "muted" {
    if (value === "ok") return "ok";
    if (value === "mismatch") return "err";
    if (value === "unverified" || value === "external") return "warn";
    return "muted";
  }

  function integrityLabel(value: string | null | undefined): string {
    const map: Record<string, string> = {
      ok: "settings.integrityOk",
      unverified: "settings.integrityUnverified",
      mismatch: "settings.integrityMismatch",
      external: "settings.integrityExternal",
      missing: "settings.integrityMissing",
      unknown: "settings.integrityUnknown",
    };
    const key = map[value ?? "unknown"];
    return key ? t(key) : (value ?? "—");
  }

  async function saveAll(): Promise<void> {
    saving = true;
    try {
      let parsedEngine: Record<string, unknown> | null = null;
      if (engineJsonDirty) {
        try {
          const parsed = JSON.parse(engineJson);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            parsedEngine = parsed as Record<string, unknown>;
          } else {
            throw new Error("invalid");
          }
        } catch {
          toast("err", t("settings.saveFailed"), t("profiles.invalidJson"));
          saving = false;
          return;
        }
      }
      await engineApi.setSettings(parsedEngine ?? {}, hostPayload());
      engineJsonDirty = false;
      await loadSettings();
      syncFromSettings();
      toast("ok", t("settings.settingsSaved"));
    } catch (err) {
      toast("err", t("settings.saveFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    } finally {
      saving = false;
    }
  }

  async function pickOutputDir(): Promise<void> {
    const folder = await pickDirectory(t("settings.outputDir"));
    if (folder) outputDir = folder;
  }

  async function pickModelsDir(): Promise<void> {
    const folder = await pickDirectory(t("settings.modelsDir"));
    if (!folder) return;
    try {
      await modelsApi.setSettings({ modelsDir: folder });
      await afterPathsChange();
    } catch (err) {
      toast("err", t("settings.saveFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  async function pickCacheDir(): Promise<void> {
    const folder = await pickDirectory(t("settings.cacheDir"));
    if (!folder) return;
    try {
      await modelsApi.setSettings({ cacheDir: folder });
      await afterPathsChange();
    } catch (err) {
      toast("err", t("settings.saveFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  async function resetModelPaths(): Promise<void> {
    try {
      await modelsApi.setSettings({ modelsDir: null, cacheDir: null });
      await afterPathsChange();
    } catch (err) {
      toast("err", t("settings.saveFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  async function changeDataDir(): Promise<void> {
    const folder = await pickDirectory(t("settings.dataDir"));
    if (!folder) return;
    try {
      await saveSettings({}, { dataDir: folder });
      toast("ok", t("datadir.saved"), folder);
      await loadHost();
      if (engine.online) await restartEngine();
      else await connect();
    } catch (err) {
      toast("err", t("settings.saveFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  async function resetDataDir(): Promise<void> {
    try {
      await saveSettings({}, { dataDir: null });
      await loadHost();
      if (engine.online) await restartEngine();
      else await connect();
    } catch (err) {
      toast("err", t("settings.saveFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  const dataDirSourceLabel = $derived.by(() => {
    const source = host?.paths.dataDirSource;
    if (source === "install") return t("settings.dataDirInstall");
    if (source === "user") return t("settings.dataDirUser");
    return t("settings.dataDirUnresolved");
  });

  /** Dopo un cambio di percorsi il sidecar va riavviato per usare i nuovi env. */
  async function afterPathsChange(): Promise<void> {
    await loadModelSettings();
    await loadModels();
    if (engine.online) await restartEngine();
    await loadHost();
  }

  async function downloadModel(model: ModelInfo): Promise<void> {
    try {
      const response = await modelsApi.download(model.id);
      setModelDownloading(model.id, true, response.jobId);
      toast("info", t("settings.downloadStarted"), model.label);
    } catch (err) {
      toast("err", t("settings.downloadFailed"), hostErrorMessage(err as { code?: string; message?: string }));
      await loadModels();
    }
  }

  async function cancelDownload(model: ModelInfo): Promise<void> {
    try {
      const response = await modelsApi.cancelDownload(model.id, engine.modelJobs[model.id]);
      if (response.cancelled) toast("info", t("settings.downloadCancelled"), model.label);
    } catch (err) {
      toast("err", t("common.error"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  function toggleModelSelection(id: string): void {
    selectedModels = selectedModels.includes(id)
      ? selectedModels.filter((item) => item !== id)
      : [...selectedModels, id];
  }

  async function deleteSelected(): Promise<void> {
    if (selectedModels.length === 0) return;
    deleting = true;
    try {
      const response = await modelsApi.remove(selectedModels);
      const deletedCount = response.deleted.length;
      const failedCount = response.failures.length;
      if (deletedCount > 0) {
        toast("ok", t("settings.modelsDeleted"), formatBytes(response.freedBytes));
      }
      if (failedCount > 0) {
        toast("warn", t("common.warning"), `${failedCount}`);
      }
      selectedModels = [];
      await loadModels();
      await loadModelSettings();
    } catch (err) {
      toast("err", t("common.deleteFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    } finally {
      deleting = false;
      pendingDelete = false;
    }
  }

  async function clearCache(): Promise<void> {
    clearingCache = true;
    try {
      const response = await modelsApi.clearCache("cache");
      toast("ok", t("settings.cacheCleared"), formatBytes(response.freedBytes));
      await loadModels();
      await loadModelSettings();
    } catch (err) {
      toast("err", t("common.error"), hostErrorMessage(err as { code?: string; message?: string }));
    } finally {
      clearingCache = false;
      pendingClearCache = false;
    }
  }

  async function runUpdateCheck(): Promise<void> {
    checkingUpdates = true;
    try {
      await checkUpdates(true);
    } finally {
      checkingUpdates = false;
    }
  }

  async function openRelease(): Promise<void> {
    const url = engine.update?.releaseUrl;
    if (!url) return;
    try {
      await engineApi.openPath(url, "file");
    } catch {
      toast("err", t("protect.openFailed"));
    }
  }

  const updateTone = $derived.by(() => {
    const info = engine.update;
    if (!info) return "muted";
    if (info.error) return "err";
    if (info.upToDate) return "ok";
    if (info.latestVersion) return "warn";
    return "muted";
  });

  const updateLabel = $derived.by(() => {
    const info = engine.update;
    if (!info) return t("common.notAvailable");
    if (info.error) return info.error;
    if (info.reason) return info.reason;
    if (info.upToDate) return t("settings.upToDate");
    if (info.latestVersion) return t("settings.updateAvailable", { v: info.latestVersion });
    return t("settings.noReleases");
  });

  onMount(() => {
    void (async () => {
      if (!engine.host) await loadHost();
      if (!engine.settings) await loadSettings();
      await loadModelSettings();
      await loadModels();
      syncFromSettings();
      if (engine.settings?.host?.autoUpdateCheck) void checkUpdates(false);
    })();
  });

  $effect(() => {
    void engine.settings;
    if (!engineJsonDirty) syncFromSettings();
  });
</script>

<div class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("settings.title")}</h1>
      <p>{t("settings.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn btn--primary btn--sm" onclick={() => void saveAll()} disabled={saving}>
        {#if saving}<span class="spinner"></span>{/if}
        {t("settings.saveSettings")}
      </button>
    </div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <div class="card__head"><h2>{t("settings.language")}</h2></div>
      <div class="card__body">
        <div class="seg" role="group" aria-label={t("settings.language")}>
          <button
            class="seg__item {i18n.lang === 'it' ? 'active' : ''}"
            onclick={() => setUiLanguage("it")}
            aria-pressed={i18n.lang === "it"}
          >
            {t("settings.italian")}
          </button>
          <button
            class="seg__item {i18n.lang === 'en' ? 'active' : ''}"
            onclick={() => setUiLanguage("en")}
            aria-pressed={i18n.lang === "en"}
          >
            {t("settings.english")}
          </button>
        </div>
        <div class="field" style="margin-top:14px">
          <span class="field__label">{t("settings.theme")}</span>
          <div class="seg" role="group" aria-label={t("settings.theme")}>
            <button
              class="seg__item {ui.theme === 'dark' ? 'active' : ''}"
              onclick={() => setUiTheme("dark")}
              aria-pressed={ui.theme === "dark"}
            >
              {t("settings.themeDark")}
            </button>
            <button
              class="seg__item {ui.theme === 'light' ? 'active' : ''}"
              onclick={() => setUiTheme("light")}
              aria-pressed={ui.theme === "light"}
            >
              {t("settings.themeLight")}
            </button>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card__head">
        <h2>{t("settings.dataSection")}</h2>
        <span class="spacer"></span>
        <Badge
          label={host?.paths.portable ? t("settings.dataDirInstall") : t("settings.dataDirUser")}
          tone={host?.paths.needsDataDir ? "err" : host?.paths.portable ? "ok" : "warn"}
        />
      </div>
      <div class="card__body">
        <dl class="kv">
          <dt>{t("settings.dataDir")}</dt>
          <dd class="mono small break-all">{host?.paths.dataDir ?? "—"}</dd>
          <dt>{t("settings.dataDirSource")}</dt>
          <dd>{dataDirSourceLabel}</dd>
          <dt>{t("settings.installDir")}</dt>
          <dd class="mono small break-all">{host?.paths.installDir ?? "—"}</dd>
          <dt>{t("settings.settingsFile")}</dt>
          <dd class="mono small break-all">{host?.paths.settingsFile ?? "—"}</dd>
        </dl>
        {#if host?.paths.needsDataDir}
          <div class="err-box small" style="margin-top:10px">{t("datadir.required")}</div>
        {:else if host?.paths.portableReason}
          <div class="hint-box small" style="margin-top:10px">{host.paths.portableReason}</div>
        {/if}
        <div class="btn-row" style="margin-top:12px">
          <button class="btn btn--sm" onclick={() => void changeDataDir()}>
            <Icon name="folder" size={14} />
            {t("settings.dataDirChange")}
          </button>
          <button
            class="btn btn--ghost btn--sm"
            onclick={() => void resetDataDir()}
            disabled={!host?.paths.installWritable}
          >
            {t("settings.dataDirReset")}
          </button>
        </div>
        <div class="hint-box small" style="margin-top:10px">{t("settings.securityHint")}</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("settings.automationSection")}</h2>
      <span class="spacer"></span>
      <Badge label={t("settings.comingSoon")} tone="muted" />
    </div>
    <div class="card__body">
      <label class="check check--disabled">
        <input type="checkbox" disabled />
        <span class="stack" style="gap:1px">
          <span>{t("settings.monitorFolder")}</span>
          <span class="small muted">{t("settings.monitorFolderHint")}</span>
          <span class="small faint">{t("settings.comingSoonHint")}</span>
        </span>
      </label>
      <div class="hint-box small" style="margin-top:12px">
        <strong>{t("settings.reaper")}</strong> — {t("settings.reaperHint")}
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("settings.modelsSection")}</h2>
      <span class="spacer"></span>
      <span class="small faint">
        {t("settings.modelsSize")}: {formatBytes(modelSettings?.downloadedBytes ?? engine.modelsMeta?.totalBytes ?? 0)}
        · {t("settings.cacheSize")}: {formatBytes(modelSettings?.cacheBytes ?? 0)}
      </span>
      <button class="btn btn--ghost btn--sm" onclick={() => void loadModels()} disabled={engine.modelsLoading}>
        <Icon name="refresh" size={14} />
        {t("settings.reloadModels")}
      </button>
    </div>
    <div class="card__body">
      <div class="grid grid-2" style="gap:12px">
        <div class="field">
          <span class="field__label">{t("settings.modelsDir")}</span>
          <div class="input-row">
            <input class="input mono" readonly value={modelSettings?.modelsDir ?? ""} />
            <button class="btn" onclick={() => void pickModelsDir()}>{t("common.browse")}</button>
          </div>
          <span class="field__help">{formatBytes(modelSettings?.downloadedBytes ?? 0)}</span>
        </div>
        <div class="field">
          <span class="field__label">{t("settings.cacheDir")}</span>
          <div class="input-row">
            <input class="input mono" readonly value={modelSettings?.cacheDir ?? ""} />
            <button class="btn" onclick={() => void pickCacheDir()}>{t("common.browse")}</button>
          </div>
          <span class="field__help">{formatBytes(modelSettings?.cacheBytes ?? 0)}</span>
        </div>
      </div>

      <div class="row" style="margin-top:10px">
        <span class="small muted">{t("settings.modelsHint")}</span>
        <span class="spacer"></span>
        <button class="btn btn--ghost btn--sm" onclick={() => void resetModelPaths()}>
          {t("settings.resetModels")}
        </button>
      </div>

      <label class="check" style="margin-top:12px">
        <input type="checkbox" bind:checked={autoPrompt} onchange={() => toast("info", t("settings.autoPromptHint"))} />
        <span class="stack" style="gap:1px">
          <span>{t("settings.autoPrompt")}</span>
          <span class="small muted">{t("settings.autoPromptHint")}</span>
        </span>
      </label>

      <div class="table-wrap" style="margin-top:14px">
        <table class="table">
          <thead>
            <tr>
              <th style="width:34px"></th>
              <th>{t("settings.modelsList")}</th>
              <th>{t("engines.title")}</th>
              <th>{t("common.size")}</th>
              <th>{t("common.status")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {#each engine.models as model (model.id)}
              <tr>
                <td>
                  <input
                    type="checkbox"
                    aria-label={model.label}
                    checked={selectedModels.includes(model.id)}
                    onchange={() => toggleModelSelection(model.id)}
                  />
                </td>
                <td>
                  <div class="bold">{model.label}</div>
                  <div class="small faint mono">{model.id} · {model.kind}</div>
                  {#if model.licenseNote}
                    <div class="small faint" style="max-width:420px">{model.licenseNote}</div>
                  {/if}
                  {#if model.reason}
                    <div class="small warn-box" style="margin-top:4px;max-width:440px">{model.reason}</div>
                  {/if}
                  {#if model.downloading}
                    <div style="margin-top:4px;max-width:220px">
                      <ProgressBar value={model.progress ?? 0} label={t("settings.downloading")} />
                    </div>
                  {/if}
                </td>
                <td class="mono small">{model.engineId}</td>
                <td class="mono small">
                  {formatBytes(model.sizeBytes)}
                  {#if model.expectedSizeBytes}
                    / {formatBytes(model.expectedSizeBytes)}
                  {/if}
                </td>
                <td>
                  <Badge
                    label={model.present ? t("settings.modelPresent") : t("settings.modelMissing")}
                    tone={model.present ? "ok" : "muted"}
                  />
                  <div style="margin-top:3px">
                    <Badge label={integrityLabel(model.integrity)} tone={integrityTone(model.integrity)} />
                  </div>
                </td>
                <td>
                  {#if model.downloading}
                    <button class="btn btn--ghost btn--sm" onclick={() => void cancelDownload(model)}>
                      {t("settings.cancelDownload")}
                    </button>
                  {:else if model.downloadAllowed && !model.present}
                    <button class="btn btn--sm" onclick={() => void downloadModel(model)}>
                      <Icon name="download" size={13} />
                      {t("settings.download")}
                    </button>
                  {:else if !model.downloadAllowed && !model.present}
                    <span class="small faint">{t("settings.weightsManualHint")}</span>
                  {:else}
                    <span class="small faint">—</span>
                  {/if}
                </td>
              </tr>
            {:else}
              <tr>
                <td colspan="6">
                  <div class="empty">{engine.modelsLoading ? t("common.loading") : t("settings.noModels")}</div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      <div class="btn-row" style="margin-top:12px">
        <button
          class="btn btn--danger btn--sm"
          disabled={selectedModels.length === 0 || deleting}
          onclick={() => (pendingDelete = true)}
        >
          <Icon name="trash" size={14} />
          {t("settings.deleteSelected")} ({selectedModels.length})
        </button>
        <button class="btn btn--sm" onclick={() => (pendingClearCache = true)} disabled={clearingCache}>
          {t("settings.clearCache")}
        </button>
        <span class="spacer"></span>
        <span class="small faint">{t("engines.downloadManual")}</span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__head"><h2>{t("settings.outputSection")}</h2></div>
    <div class="card__body">
      <div class="field">
        <span class="field__label">{t("settings.outputDir")}</span>
        <div class="input-row">
          <input class="input mono" readonly value={outputDir} />
          <button class="btn" onclick={() => void pickOutputDir()}>{t("common.browse")}</button>
        </div>
        <span class="field__help">{t("settings.outputDirHint")}</span>
      </div>
      <label class="check" style="margin-top:12px">
        <input type="checkbox" bind:checked={openAfterJob} />
        <span>{t("settings.openAfterJob")}</span>
      </label>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("settings.updatesSection")}</h2>
      <span class="spacer"></span>
      <Badge label={updateLabel} tone={updateTone} />
      <button class="btn btn--sm" onclick={() => void runUpdateCheck()} disabled={checkingUpdates}>
        {#if checkingUpdates}<span class="spinner"></span>{/if}
        {t("settings.checkUpdates")}
      </button>
    </div>
    <div class="card__body">
      <div class="warn-box small" style="margin-bottom:12px">{t("settings.updaterDisabled")}</div>
      <dl class="kv">
        <dt>{t("settings.currentVersion")}</dt>
        <dd class="mono">{engine.update?.currentVersion ?? host?.appVersion ?? "—"}</dd>
        <dt>{t("settings.latestVersion")}</dt>
        <dd class="mono">{engine.update?.latestVersion ?? "—"}</dd>
        <dt>{t("settings.lastChecked")}</dt>
        <dd class="mono small">{formatDate(engine.update?.checkedAt ?? null)}</dd>
        <dt>{t("about.repo")}</dt>
        <dd class="mono small break-all">{engine.update?.repo ?? "Nicotina4ever/NL-VO-SHIELD"}</dd>
      </dl>
      {#if engine.update?.notes}
        <details style="margin-top:10px">
          <summary class="small muted" style="cursor:pointer">{t("settings.updateNotes")}</summary>
          <pre class="mono small" style="white-space:pre-wrap;max-height:220px;overflow:auto;margin:6px 0 0">
{engine.update.notes}</pre>
        </details>
      {/if}
      <div class="btn-row" style="margin-top:12px">
        <label class="check">
          <input type="checkbox" bind:checked={autoUpdateCheck} />
          <span>{t("settings.autoCheck")}</span>
        </label>
        <span class="spacer"></span>
        {#if engine.update?.releaseUrl}
          <button class="btn btn--sm" onclick={() => void openRelease()}>
            <Icon name="external" size={13} />
            {t("settings.openRelease")}
          </button>
        {/if}
      </div>
      <span class="field__help">{t("settings.autoCheckHint")}</span>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("settings.engineSettings")}</h2>
      <span class="spacer"></span>
      {#if !engine.online}
        <span class="small faint">{t("settings.engineSettingsUnavailable")}</span>
      {/if}
    </div>
    <div class="card__body">
      <div class="field">
        <label for="engine-settings">{t("settings.rawSettings")}</label>
        <textarea
          id="engine-settings"
          class="textarea"
          rows="8"
          bind:value={engineJson}
          oninput={() => (engineJsonDirty = true)}
          spellcheck="false"
        ></textarea>
        <span class="field__help">settings.get / settings.set</span>
      </div>
    </div>
    <div class="card__foot">
      <button class="btn btn--primary btn--sm" onclick={() => void saveAll()} disabled={saving}>
        {t("settings.saveSettings")}
      </button>
      <button
        class="btn btn--ghost btn--sm"
        onclick={() => {
          engineJsonDirty = false;
          syncFromSettings();
        }}
      >
        {t("common.reset")}
      </button>
    </div>
  </div>
</div>

{#if pendingDelete}
  <Modal title={t("common.confirm")} onclose={() => (pendingDelete = false)}>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (pendingDelete = false)}>{t("common.cancel")}</button>
      <button class="btn btn--danger" onclick={() => void deleteSelected()} disabled={deleting}>
        {#if deleting}<span class="spinner"></span>{/if}
        {t("common.delete")}
      </button>
    {/snippet}
    <p>{t("settings.deleteSelected")} — {selectedModels.length}</p>
  </Modal>
{/if}

{#if pendingClearCache}
  <Modal title={t("settings.clearCache")} onclose={() => (pendingClearCache = false)}>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (pendingClearCache = false)}>{t("common.cancel")}</button>
      <button class="btn btn--primary" onclick={() => void clearCache()} disabled={clearingCache}>
        {#if clearingCache}<span class="spinner"></span>{/if}
        {t("settings.clearCache")}
      </button>
    {/snippet}
    <p>{t("settings.clearCacheConfirm")}</p>
  </Modal>
{/if}
