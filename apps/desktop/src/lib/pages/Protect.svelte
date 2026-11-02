<script lang="ts">
  import { onMount } from "svelte";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import { ui, toast, goVerify } from "../ui.svelte";
  import {
    engine,
    loadProfiles,
    loadSettings,
    engineUsableReason,
  } from "../engine.svelte";
  import {
    single,
    startSingleJob,
    cancelSingleJob,
    resetSingleView,
    goToVerifyFromResult,
  } from "../run.svelte";
  import {
    BUILTIN_PRESETS,
    buildChain,
    describeChain,
    findPreset,
    type Strength,
  } from "../presets";
  import { AUDIO_EXTENSIONS, pickAudioFile, pickDirectory, pickSavePath } from "../dialogs";
  import { engineApi, isTauri } from "../ipc";
  import { basename, fileExtension } from "../format";
  import type { JobResult } from "../types";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";
  import Waveform from "../components/Waveform.svelte";
  import ProgressBar from "../components/ProgressBar.svelte";
  import StepList from "../components/StepList.svelte";
  import JobResultView from "../components/JobResultView.svelte";

  let inputPath = $state<string | null>(null);
  let outputDir = $state("");
  let presetId = $state("studio_master");
  let antiCloneOn = $state(true);
  let watermarkOn = $state(true);
  let c2paOn = $state(true);
  let strength = $state<Strength>("high");
  let verifyAfter = $state(true);
  let payloadHex = $state("");
  let manifestTitle = $state("");
  let profileId = $state("");
  let advancedOpen = $state(false);
  let dragging = $state(false);
  let antiCloneEngine = $state("");
  let watermarkEngine = $state("");

  let lastOpenRequest = 0;
  let lastStartRequest = 0;

  const antiCloneEngines = $derived(engine.engines.filter((item) => item.kind === "anti_clone"));
  const watermarkEngines = $derived(engine.engines.filter((item) => item.kind === "watermark"));

  const preset = $derived(findPreset(presetId) ?? BUILTIN_PRESETS[0]);
  const selectedProfile = $derived(engine.profiles.find((item) => item.id === profileId) ?? null);

  const chain = $derived(
    buildChain({
      antiClone: antiCloneOn,
      watermark: watermarkOn,
      c2pa: c2paOn,
      strength,
      antiCloneEngine,
      watermarkEngine,
      payloadHex,
      manifestTitle,
      author: selectedProfile?.name ?? undefined,
      usageTerms: selectedProfile?.usage_terms ?? undefined,
    }),
  );

  const blockedReasons = $derived(
    chain
      .map((step) => ({ engine: step.engine, reason: engineUsableReason(step.engine) }))
      .filter((entry) => entry.reason !== null),
  );

  const canStart = $derived(
    !single.running &&
      Boolean(inputPath) &&
      Boolean(outputDir) &&
      chain.length > 0 &&
      blockedReasons.length === 0,
  );

  const outputWaveformPath = $derived(single.result?.output_path ?? null);

  function applyPreset(id: string): void {
    presetId = id;
    const found = findPreset(id);
    if (!found) return;
    if (!found.custom) {
      antiCloneOn = found.antiClone;
      watermarkOn = found.watermark;
      c2paOn = found.c2pa;
      strength = found.strength;
    }
  }

  function markCustom(): void {
    if (presetId !== "custom") presetId = "custom";
  }

  function acceptFile(path: string): void {
    const ext = fileExtension(path);
    if (!AUDIO_EXTENSIONS.includes(ext)) {
      toast("warn", t("protect.invalidAudio"), ext ? `.${ext}` : path);
      return;
    }
    inputPath = path;
    resetSingleView();
  }

  async function pickInput(): Promise<void> {
    const paths = await pickAudioFile({ title: t("protect.inputFile") });
    if (paths && paths.length > 0) acceptFile(paths[0]);
  }

  async function pickOutput(): Promise<void> {
    const folder = await pickDirectory(t("protect.outputDir"));
    if (folder) outputDir = folder;
  }

  async function start(): Promise<void> {
    if (single.running) return;
    if (!inputPath) {
      toast("warn", t("protect.chooseInputFirst"));
      return;
    }
    if (!outputDir) {
      toast("warn", t("protect.chooseOutputFirst"));
      return;
    }
    if (chain.length === 0) {
      toast("warn", t("protect.chainEmpty"));
      return;
    }
    if (blockedReasons.length > 0) {
      toast("err", t("protect.engineUnavailable"), blockedReasons.map((entry) => entry.engine).join(", "));
      return;
    }
    await startSingleJob({
      inputPath,
      outputDir,
      chain,
      preset: presetId,
      options: {
        verify: verifyAfter,
        profile_id: profileId || null,
        preset_id: presetId,
      },
    });
  }

  async function cancel(): Promise<void> {
    await cancelSingleJob();
  }

  async function exportReport(result: JobResult): Promise<void> {
    const defaultName = `nl-vo-shield-job-${result.job_id}.json`;
    const target = await pickSavePath({
      title: t("protect.exportReport"),
      defaultPath: defaultName,
      filters: [{ name: "JSON", extensions: ["json"] }],
    });
    if (!target) return;
    try {
      const response = await engineApi.exportReport(JSON.stringify(result, null, 2), defaultName, target);
      if (response.saved) toast("ok", t("protect.reportSaved"), response.path ?? target);
    } catch (err) {
      toast("err", t("common.error"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  function openOutputFolder(): void {
    const path = single.result?.output_path;
    if (!path) return;
    const idx = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
    const folder = idx > 0 ? path.slice(0, idx) : path;
    void engineApi.openPath(folder, "folder").catch(() => toast("err", t("protect.openFailed")));
  }

  onMount(() => {
    void (async () => {
      if (engine.profiles.length === 0) void loadProfiles();
      if (!engine.settings) void loadSettings();
    })();

    let unlisten: (() => void) | undefined;
    if (isTauri()) {
      void (async () => {
        try {
          const { getCurrentWebview } = await import("@tauri-apps/api/webview");
          unlisten = await getCurrentWebview().onDragDropEvent((event) => {
            const payload = event.payload;
            if (payload.type === "over" || payload.type === "enter") {
              dragging = true;
            } else if (payload.type === "leave") {
              dragging = false;
            } else if (payload.type === "drop") {
              dragging = false;
              const first = payload.paths?.[0];
              if (first) acceptFile(first);
            }
          });
        } catch {
          /* drag&drop non disponibile: resta il selettore file */
        }
      })();
    }
    return () => unlisten?.();
  });

  // Ctrl+O globale → apri file
  $effect(() => {
    const request = ui.openFileRequest;
    if (request === lastOpenRequest) return;
    lastOpenRequest = request;
    void pickInput();
  });

  // Ctrl+Enter globale → avvia
  $effect(() => {
    const request = ui.startRequest;
    if (request === lastStartRequest) return;
    lastStartRequest = request;
    if (ui.page === "protect") void start();
  });

  // Motori predefiniti: il primo disponibile per ciascuna categoria.
  $effect(() => {
    if (engine.engines.length === 0) return;
    if (!antiCloneEngines.some((item) => item.id === antiCloneEngine && item.status !== "unavailable")) {
      const pick =
        antiCloneEngines.find((item) => item.status !== "unavailable") ?? antiCloneEngines[0];
      if (pick && antiCloneEngine !== pick.id) antiCloneEngine = pick.id;
    }
    if (!watermarkEngines.some((item) => item.id === watermarkEngine && item.status !== "unavailable")) {
      const pick =
        watermarkEngines.find((item) => item.status !== "unavailable") ?? watermarkEngines[0];
      if (pick && watermarkEngine !== pick.id) watermarkEngine = pick.id;
    }
  });

  // Cartella di output predefinita dalla configurazione host
  $effect(() => {
    if (outputDir) return;
    const fromSettings = engine.settings?.host?.outputDir;
    if (fromSettings) {
      outputDir = fromSettings;
      return;
    }
    const fallback = engine.host?.paths.outputDir;
    if (fallback) outputDir = fallback;
  });
</script>

<div class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("protect.title")}</h1>
      <p>{t("protect.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn btn--sm" onclick={() => void pickInput()}>
        <Icon name="file" size={14} />
        {t("common.chooseFile")}
      </button>
    </div>
  </div>

  <div class="split">
    <div class="col" style="gap:16px">
      <div class="card">
        <div class="card__head">
          <h2>{t("protect.inputFile")}</h2>
          {#if inputPath}
            <span class="spacer"></span>
            <span class="small mono faint truncate" style="max-width:320px" title={inputPath}>
              {basename(inputPath)}
            </span>
          {/if}
        </div>
        <div class="card__body">
          {#if inputPath}
            <div class="col" style="gap:10px">
              <div class="row nowrap">
                <span class="mono small truncate" title={inputPath}>{inputPath}</span>
                <span class="spacer"></span>
                <button class="btn btn--ghost btn--sm" onclick={() => void pickInput()}>
                  {t("protect.replace")}
                </button>
              </div>
              <Waveform path={inputPath} label={t("protect.inputPreview")} tall />
              {#if outputWaveformPath}
                <Waveform path={outputWaveformPath} label={t("protect.outputPreview")} tall />
              {/if}
            </div>
          {:else}
            <button
              class="dropzone {dragging ? 'dropzone--active' : ''}"
              style="width:100%"
              onclick={() => void pickInput()}
              ondragover={(event) => {
                event.preventDefault();
                dragging = true;
              }}
              ondragleave={() => (dragging = false)}
              ondrop={(event) => event.preventDefault()}
            >
              <Icon name="file" size={28} />
              <span class="dropzone__title">{t("protect.dropTitle")}</span>
              <span class="small">{t("protect.dropHint")}</span>
            </button>
          {/if}

          <div class="field" style="margin-top:12px">
            <span class="field__label">{t("protect.outputDir")}</span>
            <div class="input-row">
              <input class="input" readonly value={outputDir} placeholder="—" />
              <button class="btn" onclick={() => void pickOutput()}>
                <Icon name="folder" size={14} />
                {t("common.browse")}
              </button>
            </div>
            <span class="field__help">{t("protect.outputDirHint")}</span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card__head">
          <h2>{t("protect.preset")}</h2>
          <span class="spacer"></span>
          <span class="small faint">{t(preset.descKey)}</span>
        </div>
        <div class="card__body">
          <div class="grid grid-2" style="gap:10px">
            {#each BUILTIN_PRESETS as item (item.id)}
              <button
                class="dropzone dropzone--sm {presetId === item.id ? 'dropzone--active' : ''}"
                style="cursor:pointer;align-items:flex-start;text-align:left"
                onclick={() => applyPreset(item.id)}
                aria-pressed={presetId === item.id}
              >
                <span class="dropzone__title">{t(item.nameKey)}</span>
                <span class="small {presetId === item.id ? '' : 'muted'}">{t(item.descKey)}</span>
              </button>
            {/each}
          </div>

          <div class="col" style="gap:12px;margin-top:16px">
            <div class="row">
              <span class="field__label">{t("protect.chain")}</span>
              <span class="spacer"></span>
              <span class="small faint">{describeChain(chain).join(" → ")}</span>
            </div>

            <label class="check">
              <input
                type="checkbox"
                bind:checked={antiCloneOn}
                onchange={markCustom}
              />
              <span class="stack" style="gap:1px">
                <span>{t("protect.engineAnticlone")}</span>
                <span class="small muted">{t("protect.engineAnticloneDesc")}</span>
              </span>
            </label>

            <label class="check">
              <input
                type="checkbox"
                bind:checked={watermarkOn}
                onchange={markCustom}
              />
              <span class="stack" style="gap:1px">
                <span>{t("protect.engineWatermark")}</span>
                <span class="small muted">{t("protect.engineWatermarkDesc")}</span>
              </span>
            </label>

            <label class="check">
              <input
                type="checkbox"
                bind:checked={c2paOn}
                onchange={markCustom}
              />
              <span class="stack" style="gap:1px">
                <span>{t("protect.engineC2pa")}</span>
                <span class="small muted">{t("protect.engineC2paDesc")}</span>
              </span>
            </label>

            {#if blockedReasons.length > 0}
              <div class="err-box">
                <div class="err-box__title">{t("protect.engineUnavailable")}</div>
                <ul class="small" style="margin:0;padding-left:18px">
                  {#each blockedReasons as item}
                    <li><span class="mono">{item.engine}</span>: {item.reason}</li>
                  {/each}
                </ul>
              </div>
            {/if}

            <div class="grid grid-2" style="gap:12px;margin-top:4px">
              <div class="field">
                <label for="protect-anticlone-engine">{t("protect.antiCloneEngine")}</label>
                <select
                  id="protect-anticlone-engine"
                  class="select"
                  bind:value={antiCloneEngine}
                  disabled={!antiCloneOn}
                >
                  {#each antiCloneEngines as item (item.id)}
                    <option value={item.id} disabled={item.status === "unavailable"}>
                      {item.name} · {item.status}
                    </option>
                  {:else}
                    <option value="">{t("common.notAvailable")}</option>
                  {/each}
                </select>
              </div>
              <div class="field">
                <label for="protect-watermark-engine">{t("protect.watermarkEngine")}</label>
                <select
                  id="protect-watermark-engine"
                  class="select"
                  bind:value={watermarkEngine}
                  disabled={!watermarkOn}
                >
                  {#each watermarkEngines as item (item.id)}
                    <option value={item.id} disabled={item.status === "unavailable"}>
                      {item.name} · {item.status}
                    </option>
                  {:else}
                    <option value="">{t("common.notAvailable")}</option>
                  {/each}
                </select>
              </div>
            </div>

            <div class="grid grid-2" style="gap:12px;margin-top:4px">
              <div class="field">
                <label for="protect-strength">{t("protect.strength")}</label>
                <select
                  id="protect-strength"
                  class="select"
                  bind:value={strength}
                  onchange={markCustom}
                >
                  <option value="light">{t("protect.strengthLight")}</option>
                  <option value="medium">{t("protect.strengthMedium")}</option>
                  <option value="high">{t("protect.strengthHigh")}</option>
                </select>
              </div>

              <div class="field">
                <label for="protect-profile">{t("protect.profile")}</label>
                <select id="protect-profile" class="select" bind:value={profileId}>
                  <option value="">{t("protect.profileNone")}</option>
                  {#each engine.profiles as profile (profile.id)}
                    <option value={profile.id}>{profile.name}</option>
                  {/each}
                </select>
                <span class="field__help">{t("protect.profileHint")}</span>
              </div>
            </div>

            <label class="check">
              <input type="checkbox" bind:checked={verifyAfter} />
              <span class="stack" style="gap:1px">
                <span>{t("protect.verifyAfter")}</span>
                <span class="small muted">{t("protect.verifyAfterHint")}</span>
              </span>
            </label>

            <button
              class="btn btn--ghost btn--sm"
              style="align-self:flex-start"
              onclick={() => (advancedOpen = !advancedOpen)}
              aria-expanded={advancedOpen}
            >
              <Icon name="settings" size={14} />
              {t("protect.advancedOptions")}
            </button>

            {#if advancedOpen}
              <div class="grid grid-2" style="gap:12px">
                <div class="field">
                  <label for="protect-payload">{t("protect.payloadHex")}</label>
                  <input
                    id="protect-payload"
                    class="input mono"
                    maxlength="4"
                    bind:value={payloadHex}
                    placeholder="0000"
                  />
                  <span class="field__help">{t("protect.payloadHint")}</span>
                </div>
                <div class="field">
                  <label for="protect-title">{t("protect.manifestTitle")}</label>
                  <input id="protect-title" class="input" bind:value={manifestTitle} />
                  <span class="field__help">{t("protect.manifestTitleHint")}</span>
                </div>
              </div>
            {/if}

            <div class="btn-row">
              <button class="btn btn--primary" onclick={() => void start()} disabled={!canStart}>
                {#if single.running}
                  <span class="spinner"></span>{t("protect.starting")}
                {:else}
                  <Icon name="play" size={15} />
                  {t("protect.startButton")}
                {/if}
              </button>
              {#if single.running}
                <button class="btn btn--danger" onclick={() => void cancel()} disabled={single.cancelling}>
                  <Icon name="stop" size={14} />
                  {t("protect.cancelButton")}
                </button>
              {/if}
              {#if !engine.online}
                <span class="small faint">{t("protect.engineOfflineShort")}</span>
              {/if}
            </div>
          </div>
        </div>
      </div>

      {#if single.error}
        <div class="err-box">
          <div class="err-box__title">{t("common.error")}</div>
          <div>{hostErrorMessage(single.error)}</div>
          {#if single.error.code}
            <div class="small mono faint" style="margin-top:4px">{single.error.code}</div>
          {/if}
        </div>
      {/if}

      <div class="card">
        <div class="card__head">
          <h2>{t("protect.resultTitle")}</h2>
          {#if single.result}
            <span class="spacer"></span>
            <Badge
              label={t(`status.${single.result.status}`)}
              tone={single.result.status === "protected" ? "ok" : single.result.status === "partial" ? "warn" : "err"}
            />
          {/if}
        </div>
        <div class="card__body">
          {#if single.result}
            <JobResultView
              result={single.result}
              onverify={() => goToVerifyFromResult()}
              onexport={(result) => void exportReport(result)}
            />
          {:else}
            <div class="empty">
              <div class="bold">{t("protect.noResultYet")}</div>
              <div class="small" style="margin-top:4px">{t("protect.noResultHint")}</div>
            </div>
          {/if}
        </div>
      </div>
    </div>

    <div class="col" style="gap:16px">
      <div class="card">
        <div class="card__head">
          <h2>{t("protect.progressTitle")}</h2>
          <span class="spacer"></span>
          <span class="small mono faint">{single.jobId ?? "—"}</span>
        </div>
        <div class="card__body">
          {#if single.running || single.steps.length > 0}
            <div class="col" style="gap:12px">
              <ProgressBar
                value={single.pct}
                label={single.message || t("protect.waitingEngine")}
                indeterminate={single.running && single.pct === 0}
              />
              <StepList steps={single.steps} />
            </div>
          {:else}
            <div class="empty">{t("protect.noResultHint")}</div>
          {/if}
        </div>
        {#if single.result}
          <div class="card__foot">
            <button class="btn btn--sm" onclick={openOutputFolder}>
              <Icon name="folder" size={14} />
              {t("protect.openResultFolder")}
            </button>
            <button
              class="btn btn--sm btn--ghost"
              onclick={() => {
                const path = single.result?.output_path;
                if (path) goVerify(path);
              }}
              disabled={!single.result.output_path}
            >
              {t("protect.verifyOutput")}
            </button>
          </div>
        {/if}
      </div>

      <div class="card">
        <div class="card__head">
          <h2>{t("nav.engines")}</h2>
        </div>
        <div class="card__body">
          <div class="col" style="gap:8px">
            {#each engine.engines as item (item.id)}
              <div class="row gap-sm small">
                <span
                  class="dot {item.status === 'unavailable'
                    ? 'dot--err'
                    : item.status === 'experimental'
                      ? 'dot--warn'
                      : 'dot--ok'}"
                ></span>
                <span class="mono truncate">{item.id}</span>
                <span class="spacer"></span>
                <span class="faint">{item.status}</span>
              </div>
            {:else}
              <span class="small faint">{engine.enginesLoading ? t("common.loading") : t("engines.noEngines")}</span>
            {/each}
            <div class="small faint" style="margin-top:6px">
              {t("protect.chainHint")}
            </div>
          </div>
        </div>
      </div>

      {#if inputPath}
        <div class="card">
          <div class="card__head">
            <h2>{t("common.details")}</h2>
          </div>
          <div class="card__body">
            <dl class="kv">
              <dt>{t("common.file")}</dt>
              <dd class="mono small break-all">{basename(inputPath)}</dd>
              <dt>{t("verify.duration")}</dt>
              <dd class="mono">{single.result ? "—" : t("common.notAvailable")}</dd>
              <dt>{t("protect.chain")}</dt>
              <dd class="small">{chain.length}</dd>
            </dl>
          </div>
        </div>
      {/if}
    </div>
  </div>
</div>
