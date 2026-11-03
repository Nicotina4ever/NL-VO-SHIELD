<script lang="ts">
  import { onMount } from "svelte";
  import { engineStateLabel, t, hostErrorMessage } from "../i18n.svelte";
  import { ui, toast } from "../ui.svelte";
  import { engine, engineUsableReason, loadSettings, loadProfiles } from "../engine.svelte";
  import { batch, startBatchJob, cancelBatchJob } from "../run.svelte";
  import {
    BUILTIN_PRESETS,
    buildChain,
    chainWarnings,
    findPreset,
    resolvePresetSelections,
    roleForKind,
    actionForRole,
    type Selection,
    type Strength,
    type StepRole,
  } from "../presets";
  import { AUDIO_EXTENSIONS, pickAudioFile, pickDirectory } from "../dialogs";
  import { engineApi, isTauri } from "../ipc";
  import { basename, fileExtension, formatPct } from "../format";
  import Icon from "../components/Icon.svelte";
  import ProgressBar from "../components/ProgressBar.svelte";
  import Badge from "../components/Badge.svelte";

  let queue = $state<string[]>([]);
  let outputDir = $state("");
  let presetId = $state("balanced_demo");
  let selections = $state<Selection[]>([]);
  let strength = $state<Strength>("light");
  let profileId = $state("");
  let dragging = $state(false);
  let lastOpenRequest = 0;
  let defaultsApplied = false;

  const selectedProfile = $derived(engine.profiles.find((item) => item.id === profileId) ?? null);

  const chainEngines = $derived(engine.engines.filter((item) => roleForKind(item.kind) !== null));

  const chain = $derived(
    buildChain(selections, {
      strength,
      author: selectedProfile?.name ?? undefined,
      usageTerms: selectedProfile?.usage_terms ?? undefined,
    }),
  );

  const warnings = $derived(chainWarnings(selections, strength));

  const blockedReasons = $derived(
    chain
      .map((step) => ({ engine: step.engine, reason: engineUsableReason(step.engine) }))
      .filter((entry) => entry.reason !== null),
  );

  const canStart = $derived(
    !batch.running && queue.length > 0 && Boolean(outputDir) && chain.length > 0 && blockedReasons.length === 0,
  );

  const completedCount = $derived(batch.items.filter((item) => item.status === "done").length);
  const failedCount = $derived(batch.items.filter((item) => item.status === "failed").length);

  function roleLabel(role: StepRole): string {
    if (role === "anti_clone") return t("protect.roleAntiClone");
    if (role === "watermark") return t("protect.roleWatermark");
    return t("protect.roleProvenance");
  }

  function applyPreset(id: string): void {
    presetId = id;
    const found = findPreset(id);
    if (!found) return;
    strength = found.strength;
    selections = resolvePresetSelections(found, engine.engines);
  }

  function toggleEngine(engineId: string, role: StepRole): void {
    const exists = selections.some((selection) => selection.engine === engineId);
    selections = exists
      ? selections.filter((selection) => selection.engine !== engineId)
      : [...selections, { engine: engineId, role }];
    presetId = "custom";
  }

  function isSelected(engineId: string): boolean {
    return selections.some((selection) => selection.engine === engineId);
  }

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
    if (defaultsApplied) return;
    if (engine.engines.length === 0) return;
    defaultsApplied = true;
    applyPreset("balanced_demo");
  });

  $effect(() => {
    const request = ui.openFileRequest;
    if (request === lastOpenRequest) return;
    lastOpenRequest = request;
    if (ui.page === "batch") void pickFiles();
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
          <div class="grid grid-2" style="gap:10px">
            {#each BUILTIN_PRESETS.filter((item) => !item.custom) as item (item.id)}
              <button
                class="dropzone dropzone--sm {presetId === item.id ? 'dropzone--active' : ''}"
                style="cursor:pointer;align-items:flex-start;text-align:left"
                onclick={() => applyPreset(item.id)}
                aria-pressed={presetId === item.id}
              >
                <span class="row gap-sm" style="align-items:center">
                  <span class="badge badge--brand">{item.level}/3</span>
                  <span class="dropzone__title">{t(item.nameKey)}</span>
                </span>
                <span class="small {presetId === item.id ? '' : 'muted'}">{t(item.descKey)}</span>
              </button>
            {/each}
          </div>

          <div class="row" style="margin-top:14px">
            <span class="field__label">{t("protect.chainEngines")}</span>
            <span class="spacer"></span>
            <span class="small faint mono">
              {chain.map((step) => `${step.engine}.${step.action}`).join(" → ") || "—"}
            </span>
          </div>
          <div class="col" style="gap:4px;margin-top:6px">
            {#each chainEngines as item (item.id)}
              {@const role = roleForKind(item.kind)}
              {#if role}
                {@const usable = item.status !== "unavailable"}
                <label class="check {usable ? '' : 'check--disabled'}">
                  <input
                    type="checkbox"
                    checked={isSelected(item.id)}
                    disabled={!usable}
                    onchange={() => toggleEngine(item.id, role)}
                  />
                  <span class="row gap-sm">
                    <span class="small">{item.name}</span>
                    <span class="mono small faint">{roleLabel(role)}.{actionForRole(role)}</span>
                    <Badge
                      label={engineStateLabel(item.status)}
                      tone={item.status === "unavailable" ? "err" : item.status === "experimental" ? "warn" : "ok"}
                    />
                  </span>
                </label>
              {/if}
            {/each}
          </div>

          {#if warnings.length > 0}
            <div class="warn-box small" style="margin-top:10px">
              <ul style="margin:0;padding-left:18px">
                {#each warnings as warning}
                  <li>{t(warning.key)}</li>
                {/each}
              </ul>
            </div>
          {/if}

          <div class="grid grid-2" style="gap:12px;margin-top:12px">
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
