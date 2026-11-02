<script lang="ts">
  import { onMount } from "svelte";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import { ui, toast } from "../ui.svelte";
  import { engine, engineUsableReason, loadSettings, loadProfiles } from "../engine.svelte";
  import { batch, startBatchJob, cancelBatchJob } from "../run.svelte";
  import { BUILTIN_PRESETS, buildChain, findPreset, type Strength } from "../presets";
  import { AUDIO_EXTENSIONS, pickAudioFile, pickDirectory } from "../dialogs";
  import { engineApi, isTauri } from "../ipc";
  import { basename, fileExtension, formatPct } from "../format";
  import Icon from "../components/Icon.svelte";
  import ProgressBar from "../components/ProgressBar.svelte";
  import Badge from "../components/Badge.svelte";

  let queue = $state<string[]>([]);
  let outputDir = $state("");
  let presetId = $state("public_distribution");
  let strength = $state<Strength>("medium");
  let profileId = $state("");
  let antiCloneEngine = $state("");
  let watermarkEngine = $state("");
  let dragging = $state(false);
  let lastOpenRequest = 0;

  const antiCloneEngines = $derived(engine.engines.filter((item) => item.kind === "anti_clone"));
  const watermarkEngines = $derived(engine.engines.filter((item) => item.kind === "watermark"));

  const preset = $derived(findPreset(presetId) ?? BUILTIN_PRESETS[2]);
  const selectedProfile = $derived(engine.profiles.find((item) => item.id === profileId) ?? null);

  const chain = $derived(
    buildChain({
      antiClone: preset.antiClone,
      watermark: preset.watermark,
      c2pa: preset.c2pa,
      strength,
      antiCloneEngine,
      watermarkEngine,
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
    !batch.running && queue.length > 0 && Boolean(outputDir) && blockedReasons.length === 0,
  );

  const completedCount = $derived(
    batch.items.filter((item) => item.status === "done").length,
  );
  const failedCount = $derived(batch.items.filter((item) => item.status === "failed").length);

  function addFiles(paths: string[]): void {
    const accepted: string[] = [];
    for (const path of paths) {
      if (!AUDIO_EXTENSIONS.includes(fileExtension(path))) continue;
      if (queue.includes(path)) continue;
      accepted.push(path);
    }
    if (accepted.length === 0) {
      toast("warn", t("protect.invalidAudio"));
      return;
    }
    queue = [...queue, ...accepted];
  }

  async function pickFiles(): Promise<void> {
    const paths = await pickAudioFile({ multiple: true, title: t("batch.inputs") });
    if (paths && paths.length > 0) addFiles(paths);
  }

  async function pickOutput(): Promise<void> {
    const folder = await pickDirectory(t("batch.outputDir"));
    if (folder) outputDir = folder;
  }

  async function start(): Promise<void> {
    if (!canStart) {
      if (blockedReasons.length > 0) {
        toast("err", t("protect.engineUnavailable"), blockedReasons.map((entry) => entry.engine).join(", "));
      }
      return;
    }
    await startBatchJob({
      inputs: queue,
      outputDir,
      chain,
      preset: presetId,
      options: { profile_id: profileId || null, preset_id: presetId },
    });
  }

  function statusLabel(status: string): string {
    if (status === "done") return t("batch.doneStatus");
    if (status === "failed") return t("batch.errorStatus");
    if (status === "running") return t("batch.current");
    if (status === "cancelled") return t("status.cancelled");
    return t("batch.queuedStatus");
  }

  function statusTone(status: string): "ok" | "warn" | "err" | "info" | "muted" {
    if (status === "done") return "ok";
    if (status === "failed") return "err";
    if (status === "running") return "info";
    if (status === "cancelled") return "warn";
    return "muted";
  }

  async function openOutputFolder(): Promise<void> {
    if (!outputDir) return;
    try {
      await engineApi.openPath(outputDir, "folder");
    } catch {
      toast("err", t("protect.openFailed"));
    }
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
            if (payload.type === "over" || payload.type === "enter") dragging = true;
            else if (payload.type === "leave") dragging = false;
            else if (payload.type === "drop") {
              dragging = false;
              if (payload.paths?.length) addFiles(payload.paths);
            }
          });
        } catch {
          /* drag&drop non disponibile */
        }
      })();
    }
    return () => unlisten?.();
  });

  $effect(() => {
    const request = ui.openFileRequest;
    if (request === lastOpenRequest) return;
    lastOpenRequest = request;
    if (ui.page === "batch") void pickFiles();
  });

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
      <h1>{t("batch.title")}</h1>
      <p>{t("batch.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn btn--sm" onclick={() => void pickFiles()}>
        <Icon name="plus" size={14} />
        {t("batch.addFiles")}
      </button>
    </div>
  </div>

  <div class="split">
    <div class="col" style="gap:16px">
      <div class="card">
        <div class="card__head">
          <h2>{t("batch.inputs")}</h2>
          <span class="spacer"></span>
          <span class="small faint">{t("batch.filesCount", { n: queue.length })}</span>
          {#if queue.length > 0 && !batch.running}
            <button class="btn btn--ghost btn--sm" onclick={() => (queue = [])}>
              {t("batch.clearQueue")}
            </button>
          {/if}
        </div>
        <div class="card__body">
          <button
            class="dropzone dropzone--sm {dragging ? 'dropzone--active' : ''}"
            style="width:100%"
            onclick={() => void pickFiles()}
          >
            <Icon name="batch" size={24} />
            <span class="dropzone__title">{t("batch.dropTitle")}</span>
            <span class="small">{t("batch.dropHint")}</span>
          </button>

          {#if queue.length === 0}
            <div class="empty">{t("batch.queueEmpty")}</div>
          {:else}
            <div class="col" style="gap:0;margin-top:10px">
              {#each queue as path, index (path)}
                <div class="list-row">
                  <span class="mono small faint">{String(index + 1).padStart(2, "0")}</span>
                  <span class="list-row__main mono small truncate" title={path}>{basename(path)}</span>
                  {#if !batch.running}
                    <button
                      class="btn btn--ghost btn--sm"
                      onclick={() => (queue = queue.filter((item) => item !== path))}
                      aria-label={t("batch.removeFromQueue")}
                      title={t("batch.removeFromQueue")}
                    >
                      ✕
                    </button>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}

          <div class="field" style="margin-top:12px">
            <span class="field__label">{t("batch.outputDir")}</span>
            <div class="input-row">
              <input class="input" readonly value={outputDir} />
              <button class="btn" onclick={() => void pickOutput()}>
                <Icon name="folder" size={14} />
                {t("common.browse")}
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card__head">
          <h2>{t("batch.presetForBatch")}</h2>
        </div>
        <div class="card__body">
          <div class="grid grid-2" style="gap:12px">
            <div class="field">
              <label for="batch-preset">{t("protect.preset")}</label>
              <select id="batch-preset" class="select" bind:value={presetId}>
                {#each BUILTIN_PRESETS as item (item.id)}
                  {#if !item.custom}
                    <option value={item.id}>{t(item.nameKey)}</option>
                  {/if}
                {/each}
              </select>
              <span class="field__help">{t(preset.descKey)}</span>
            </div>
            <div class="field">
              <label for="batch-strength">{t("protect.strength")}</label>
              <select id="batch-strength" class="select" bind:value={strength}>
                <option value="light">{t("protect.strengthLight")}</option>
                <option value="medium">{t("protect.strengthMedium")}</option>
                <option value="high">{t("protect.strengthHigh")}</option>
              </select>
            </div>
            <div class="field">
              <label for="batch-profile">{t("protect.profile")}</label>
              <select id="batch-profile" class="select" bind:value={profileId}>
                <option value="">{t("protect.profileNone")}</option>
                {#each engine.profiles as profile (profile.id)}
                  <option value={profile.id}>{profile.name}</option>
                {/each}
              </select>
            </div>
            <div class="field">
              <label for="batch-watermark-engine">{t("protect.watermarkEngine")}</label>
              <select id="batch-watermark-engine" class="select" bind:value={watermarkEngine}>
                {#each watermarkEngines as item (item.id)}
                  <option value={item.id} disabled={item.status === "unavailable"}>
                    {item.name} · {item.status}
                  </option>
                {:else}
                  <option value="">{t("common.notAvailable")}</option>
                {/each}
              </select>
            </div>
            <div class="field">
              <label for="batch-anticlone-engine">{t("protect.antiCloneEngine")}</label>
              <select id="batch-anticlone-engine" class="select" bind:value={antiCloneEngine}>
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
              <span class="field__label">{t("protect.chain")}</span>
              <span class="small mono muted">
                {chain.map((step) => `${step.engine}.${step.action}`).join(" → ") || t("common.none")}
              </span>
            </div>
          </div>

          {#if blockedReasons.length > 0}
            <div class="err-box" style="margin-top:12px">
              <div class="err-box__title">{t("protect.engineUnavailable")}</div>
              <ul class="small" style="margin:0;padding-left:18px">
                {#each blockedReasons as item}
                  <li><span class="mono">{item.engine}</span>: {item.reason}</li>
                {/each}
              </ul>
            </div>
          {/if}

          <div class="btn-row" style="margin-top:14px">
            <button class="btn btn--primary" onclick={() => void start()} disabled={!canStart}>
              {#if batch.running}
                <span class="spinner"></span>{t("common.running")}
              {:else}
                <Icon name="play" size={15} />
                {t("batch.startBatch")}
              {/if}
            </button>
            {#if batch.running}
              <button class="btn btn--danger" onclick={() => void cancelBatchJob()} disabled={batch.cancelling}>
                <Icon name="stop" size={14} />
                {t("batch.stopBatch")}
              </button>
            {/if}
            {#if outputDir}
              <button class="btn btn--ghost btn--sm" onclick={() => void openOutputFolder()}>
                <Icon name="folder" size={14} />
                {t("common.openFolder")}
              </button>
            {/if}
          </div>
        </div>
      </div>
    </div>

    <div class="col" style="gap:16px">
      <div class="card">
        <div class="card__head">
          <h2>{t("batch.progressTitle")}</h2>
          <span class="spacer"></span>
          <span class="small mono faint">{batch.jobId ?? "—"}</span>
        </div>
        <div class="card__body">
          <ProgressBar
            value={batch.pct}
            label={batch.message || t("batch.aggregate")}
            indeterminate={batch.running && batch.pct === 0}
          />
          <div class="row" style="margin-top:12px;gap:18px">
            <div>
              <div class="bold">{completedCount}</div>
              <div class="small muted">{t("batch.completed")}</div>
            </div>
            <div>
              <div class="bold">{failedCount}</div>
              <div class="small muted">{t("batch.failed")}</div>
            </div>
            <div>
              <div class="bold">{batch.items.length}</div>
              <div class="small muted">{t("common.total")}</div>
            </div>
          </div>
          {#if batch.error}
            <div class="err-box" style="margin-top:12px">
              <div class="err-box__title">{t("common.error")}</div>
              <div>{hostErrorMessage(batch.error)}</div>
            </div>
          {/if}
        </div>
      </div>

      <div class="card">
        <div class="card__head">
          <h2>{t("batch.queue")}</h2>
        </div>
        <div class="card__body card__body--flush">
          {#if batch.items.length === 0}
            <div class="empty">{t("batch.noBatchResult")}</div>
          {:else}
            <table class="table">
              <thead>
                <tr>
                  <th>{t("common.file")}</th>
                  <th>{t("batch.itemStatus")}</th>
                  <th style="width:84px">{t("common.total")}</th>
                </tr>
              </thead>
              <tbody>
                {#each batch.items as item (item.path)}
                  <tr>
                    <td class="truncate mono small" style="max-width:220px" title={item.path}>
                      {basename(item.path)}
                    </td>
                    <td>
                      <Badge label={statusLabel(item.status)} tone={statusTone(item.status)} />
                      {#if item.error}
                        <div class="small faint truncate" style="max-width:200px">{item.error}</div>
                      {/if}
                    </td>
                    <td class="mono small">{formatPct(item.pct)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}
        </div>
      </div>
    </div>
  </div>
</div>
