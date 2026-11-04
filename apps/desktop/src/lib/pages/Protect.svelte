<script lang="ts">
  import { onMount } from "svelte";
  import { engineStateLabel, t, hostErrorMessage } from "../i18n.svelte";
  import { ui, toast, goVerify, navigate } from "../ui.svelte";
  import {
    engine,
    loadProfiles,
    loadSettings,
    loadPresets,
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
    chainWarnings,
    describeChain,
    findPreset,
    roleForKind,
    resolvePresetSelections,
    actionForRole,
    type Selection,
    type Strength,
    type StepRole,
  } from "../presets";
  import { AUDIO_EXTENSIONS, pickAudioFile, pickDirectory, pickSavePath } from "../dialogs";
  import { engineApi, isTauri } from "../ipc";
  import { basename, fileExtension } from "../format";
  import type { JobResult, Preset } from "../types";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";
  import Waveform from "../components/Waveform.svelte";
  import ProgressBar from "../components/ProgressBar.svelte";
  import StepList from "../components/StepList.svelte";
  import JobResultView from "../components/JobResultView.svelte";
  import AudioPlayer from "../components/AudioPlayer.svelte";

  let inputPath = $state<string | null>(null);
  let outputDir = $state("");
  let presetId = $state("balanced_demo");
  let selections = $state<Selection[]>([]);
  let strength = $state<Strength>("light");
  let verifyAfter = $state(true);
  let payloadHex = $state("");
  let manifestTitle = $state("");
  let engineParams = $state<Record<string, Record<string, unknown>>>({});
  let profileId = $state("");
  let advancedOpen = $state(false);
  let dragging = $state(false);
  let presetFallback = $state(false);

  let lastOpenRequest = 0;
  let lastStartRequest = 0;
  let defaultsApplied = false;

  const preset = $derived(findPreset(presetId) ?? BUILTIN_PRESETS[1]);
  const selectedProfile = $derived(engine.profiles.find((item) => item.id === profileId) ?? null);

  const chainEngines = $derived(
    engine.engines.filter((item) => roleForKind(item.kind) !== null),
  );

  const chain = $derived(
    buildChain(selections, {
      strength,
      payloadHex,
      manifestTitle,
      author: selectedProfile?.name ?? undefined,
      engineParams,
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
    !single.running &&
      Boolean(inputPath) &&
      Boolean(outputDir) &&
      chain.length > 0 &&
      blockedReasons.length === 0,
  );

  const outputWaveformPath = $derived(single.result?.output_path ?? null);
  const listenTracks = $derived(
    inputPath && single.result?.output_path
      ? [
          { label: t("player.input"), path: inputPath },
          { label: t("player.output"), path: single.result.output_path },
        ]
      : inputPath
        ? [{ label: t("player.input"), path: inputPath }]
        : [],
  );

  function roleLabel(role: StepRole): string {
    if (role === "anti_clone") return t("protect.roleAntiClone");
    if (role === "watermark") return t("protect.roleWatermark");
    return t("protect.roleProvenance");
  }

  function roleOrder(role: StepRole): number {
    return role === "anti_clone" ? 0 : role === "watermark" ? 1 : 2;
  }

  function orderedSelections(): Selection[] {
    return [...selections].sort((left, right) => {
      const order = roleOrder(left.role) - roleOrder(right.role);
      return order === 0 ? selections.indexOf(left) - selections.indexOf(right) : order;
    });
  }

  function applyPreset(id: string): void {
    presetId = id;
    const found = findPreset(id);
    if (!found) return;
    if (found.custom) {
      strength = "medium";
      return;
    }
    strength = found.strength;
    const resolved = resolvePresetSelections(found, engine.engines);
    presetFallback =
      resolved.length !== found.engines.length ||
      resolved.some((selection) => !found.engines.includes(selection.engine));
    selections = resolved;
  }

  function applyEnginePreset(presetItem: Preset): void {
    const found = findPreset(presetId);
    void found;
    const next: Selection[] = [];
    for (const step of (presetItem.chain as { engine: string }[] | null) ?? []) {
      const info = engine.engines.find((item) => item.id === step.engine);
      const role = roleForKind(info?.kind) ?? "watermark";
      if (!next.some((selection) => selection.engine === step.engine)) {
        next.push({ engine: step.engine, role });
      }
    }
    selections = next;
    presetId = "custom";
    toast("info", t("protect.presetApplied"), presetItem.name);
  }

  function toggleEngine(engineId: string, role: StepRole): void {
    const exists = selections.some((selection) => selection.engine === engineId);
    selections = exists
      ? selections.filter((selection) => selection.engine !== engineId)
      : [...selections, { engine: engineId, role }];
    if (presetId !== "custom") presetId = "custom";
  }

  function canMove(engineId: string, direction: -1 | 1): boolean {
    const ordered = orderedSelections();
    const index = ordered.findIndex((selection) => selection.engine === engineId);
    if (index < 0) return false;
    const target = ordered[index + direction];
    return Boolean(target && target.role === ordered[index].role);
  }

  function moveSelection(engineId: string, direction: -1 | 1): void {
    const ordered = orderedSelections();
    const index = ordered.findIndex((selection) => selection.engine === engineId);
    if (index < 0) return;
    const current = ordered[index];
    const targetIndex = index + direction;
    if (targetIndex < 0 || targetIndex >= ordered.length) return;
    const target = ordered[targetIndex];
    if (target.role !== current.role) return;
    const byEngine = (selection: Selection) => selection.engine;
    const currentIndex = selections.findIndex((selection) => selection.engine === byEngine(current));
    const targetPos = selections.findIndex((selection) => selection.engine === byEngine(target));
    const next = [...selections];
    next[currentIndex] = target;
    next[targetPos] = current;
    selections = next;
    if (presetId !== "custom") presetId = "custom";
  }

  function setParam(engineId: string, key: string, value: unknown): void {
    engineParams = { ...engineParams, [engineId]: { ...(engineParams[engineId] ?? {}), [key]: value } };
    if (presetId !== "custom") presetId = "custom";
  }

  function isSelected(engineId: string): boolean {
    return selections.some((selection) => selection.engine === engineId);
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
      if (engine.presets.length === 0) void loadPresets();
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

  // preset iniziale quando il registro motori è disponibile
  $effect(() => {
    if (defaultsApplied) return;
    if (engine.engines.length === 0) return;
    defaultsApplied = true;
    applyPreset("balanced_demo");
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

<div class="page mode-{ui.viewMode}">
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

  {#if ui.viewMode === "simple"}
    <div class="card" style="margin-bottom:14px;border-color:var(--nl-accent)">
      <div class="card__body col" style="gap:14px">
        <div class="row gap-sm" style="align-items:center">
          <span class="badge badge--brand">1</span>
          <div><div class="bold">{t("protect.inputFile")}</div><div class="small muted">{t("protect.dropHint")}</div></div>
          <span class="spacer"></span>
          <button class="btn btn--primary" onclick={() => void pickInput()}>{t("common.chooseFile")}</button>
        </div>
        <div class="row gap-sm" style="align-items:center">
          <span class="badge badge--brand">2</span>
          <div><div class="bold">{t("protect.preset")}</div><div class="small muted">{t(preset.descKey)}</div></div>
        </div>
        <div class="row gap-sm" style="align-items:center">
          <span class="badge badge--brand">3</span>
          <div><div class="bold">{t("protect.startProtection")}</div><div class="small muted">{t("protect.qualityHint")}</div></div>
          <span class="spacer"></span>
          <button class="btn btn--primary" onclick={() => void start()} disabled={!canStart}>{t("protect.startProtection")}</button>
        </div>
        {#if blockedReasons.length > 0}
          <div class="warn-box">
            <div class="warn-box__title">{t("protect.engineUnavailable")}</div>
            <ul class="small" style="margin:6px 0;padding-left:18px">
              {#each blockedReasons as item}<li>{item.engine}: {item.reason}</li>{/each}
            </ul>
            <button class="btn btn--sm" onclick={() => navigate("components")}>{t("components.title")}</button>
          </div>
        {/if}
        {#if chain.map((s) => s.engine).includes("c2pa") === false}
          <div class="small muted">{t("protect.warnNoC2pa")}</div>
        {/if}
      </div>
    </div>
  {/if}

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
              <Waveform path={inputPath} label={t("protect.inputPreview")} />
              {#if outputWaveformPath}
                <Waveform path={outputWaveformPath} label={t("protect.outputPreview")} />
              {/if}
            </div>
          {:else}
            <button
              class="dropzone {dragging ? 'dropzone--active' : ''}"
              style="width:100%"
              onclick={() => void pickInput()}
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
          <span class="small faint">{t(preset.impactKey)}</span>
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
                <span class="row gap-sm" style="align-items:center">
                  {#if item.level > 0}<span class="badge badge--brand">{item.level}/3</span>{/if}
                  <span class="dropzone__title">{t(item.nameKey)}</span>
                </span>
                <span class="small {presetId === item.id ? '' : 'muted'}">{t(item.descKey)}</span>
                <span class="small faint">{t(item.impactKey)}</span>
              </button>
            {/each}
          </div>

          {#if presetFallback}
            <div class="hint-box small" style="margin-top:10px">{t("protect.presetFallback")}</div>
          {/if}

          {#if engine.presets.length > 0}
            <div class="col" style="gap:6px;margin-top:12px">
              <span class="field__label">{t("protect.enginePresets")}</span>
              <div class="chip-list">
                {#each engine.presets as presetItem (presetItem.id)}
                  <button class="btn btn--ghost btn--sm" onclick={() => applyEnginePreset(presetItem)}>
                    {presetItem.name}
                  </button>
                {/each}
              </div>
            </div>
          {/if}

          <div class="row" style="margin-top:16px">
            <span class="field__label">{t("protect.chainEngines")}</span>
            <span class="spacer"></span>
            <span class="small faint mono">{describeChain(chain).join(" → ") || "—"}</span>
          </div>
          {#if ui.viewMode !== "simple"}<p class="small muted" style="margin:4px 0 10px">{t("protect.chainOrderHint")}</p>{/if}

          <div class="col expert-only" style="gap:6px">
            {#each orderedSelections() as selection (selection.engine)}
              {@const info = engine.engines.find((item) => item.id === selection.engine)}
              <div class="list-row">
                <input
                  type="checkbox"
                  checked={isSelected(selection.engine)}
                  onchange={() => toggleEngine(selection.engine, selection.role)}
                  aria-label={selection.engine}
                />
                <div class="list-row__main">
                  <div class="row gap-sm">
                    <span class="chip">{roleLabel(selection.role)}</span>
                    <span class="mono small">{selection.engine}.{actionForRole(selection.role)}</span>
                    {#if info}
                      <Badge
                        label={engineStateLabel(info.status)}
                        tone={info.status === "unavailable" ? "err" : info.status === "experimental" ? "warn" : "ok"}
                      />
                    {/if}
                  </div>
                  <div class="small faint">
                    {info?.name ?? selection.engine} · {info?.version ?? "—"}
                    {#if info?.reason}· {info.reason}{/if}
                  </div>
                </div>
                <div class="row gap-sm">
                  <button
                    class="btn btn--ghost btn--sm"
                    onclick={() => moveSelection(selection.engine, -1)}
                    disabled={!canMove(selection.engine, -1)}
                    title={t("protect.moveUp")}
                    aria-label={t("protect.moveUp")}
                  >
                    ↑
                  </button>
                  <button
                    class="btn btn--ghost btn--sm"
                    onclick={() => moveSelection(selection.engine, 1)}
                    disabled={!canMove(selection.engine, 1)}
                    title={t("protect.moveDown")}
                    aria-label={t("protect.moveDown")}
                  >
                    ↓
                  </button>
                </div>
              </div>
            {:else}
              <div class="empty">{t("protect.warnEmptyChain")}</div>
            {/each}
          </div>

          {#if chainEngines.length > 0}
            <div class="col" style="gap:4px;margin-top:12px">
              <span class="small faint">{t("protect.engineListHint")}</span>
              {#each chainEngines as item (item.id)}
                {@const role = roleForKind(item.kind)}
                {#if role}
                  {@const usable = item.status !== "unavailable"}
                  {@const selected = isSelected(item.id)}
                  <label class="check {usable ? '' : 'check--disabled'}">
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={!usable}
                      onchange={() => toggleEngine(item.id, role)}
                    />
                    <span class="row gap-sm">
                      <span class="small">{item.name}</span>
                      <span class="mono small faint">{roleLabel(role)}</span>
                      <Badge
                        label={engineStateLabel(item.status)}
                        tone={item.status === "unavailable" ? "err" : item.status === "experimental" ? "warn" : "ok"}
                      />
                      {#if !usable && item.reason}
                        <span class="small faint">{item.reason}</span>
                      {/if}
                    </span>
                  </label>
                {/if}
              {/each}
            </div>
          {/if}

          {#if warnings.length > 0}
            <div class="warn-box small" style="margin-top:12px">
              <ul style="margin:0;padding-left:18px">
                {#each warnings as warning}
                  <li class={warning.level === "error" ? "field__error" : ""}>{t(warning.key)}</li>
                {/each}
              </ul>
            </div>
          {/if}

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

          <div class="grid grid-2 expert-only" style="gap:12px;margin-top:14px">
            <div class="field">
              <label for="protect-strength">{t("protect.strength")}</label>
              <select
                id="protect-strength"
                class="select"
                bind:value={strength}
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

          <label class="check" style="margin-top:12px">
            <input type="checkbox" bind:checked={verifyAfter} />
            <span class="stack" style="gap:1px">
              <span>{t("protect.verifyAfter")}</span>
              <span class="small muted">{t("protect.verifyAfterHint")}</span>
            </span>
          </label>

          {#if ui.viewMode === "advanced"}
          <button
            class="btn btn--ghost btn--sm"
            style="align-self:flex-start;margin-top:8px"
            onclick={() => (advancedOpen = !advancedOpen)}
            aria-expanded={advancedOpen}
          >
            <Icon name="settings" size={14} />
            {t("protect.advancedOptions")}
          </button>
          {/if}

          {#if advancedOpen && ui.viewMode === "advanced"}
            <div class="grid grid-2" style="gap:12px;margin-top:10px">
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

          {#if ui.viewMode === "advanced" && selections.some((s) => s.role === "anti_clone")}
            <div class="card" style="margin-top:12px">
              <div class="card__head"><h2>{t("protect.advancedOptions")}</h2></div>
              <div class="card__body col" style="gap:10px">
                {#each selections.filter((s) => s.role === "anti_clone") as sel (sel.engine)}
                  {@const p = engineParams[sel.engine] ?? {}}
                  <div class="field">
                    <span class="field__label">{sel.engine}</span>
                    <div class="row gap-sm" style="flex-wrap:wrap">
                      {#if sel.engine === "safespeech" || sel.engine === "e2e_vguard"}
                        <label class="small">epochs <input class="input" type="number" min="1" max="500" value={String(p.epochs ?? 10)} oninput={(e) => setParam(sel.engine, "epochs", Number(e.currentTarget.value))} /></label>
                        <label class="small">epsilon <input class="input" type="number" min="1" max="64" value={String(p.epsilon ?? 8)} oninput={(e) => setParam(sel.engine, "epsilon", Number(e.currentTarget.value))} /></label>
                      {/if}
                      {#if sel.engine === "mimic_blocker" || sel.engine === "enkidu"}
                        <label class="small">target SNR dB <input class="input" type="number" min="6" max="40" value={String(p.target_snr_db ?? 20)} oninput={(e) => setParam(sel.engine, "target_snr_db", Number(e.currentTarget.value))} /></label>
                      {/if}
                      {#if sel.engine === "mimic_blocker"}
                        <label class="small">mix <input class="input" type="number" min="0" max="1" step="0.05" value={String(p.mix ?? 0.5)} oninput={(e) => setParam(sel.engine, "mix", Number(e.currentTarget.value))} /></label>
                      {/if}
                      {#if sel.engine === "enkidu"}
                        <label class="small">steps <input class="input" type="number" min="1" max="100" value={String(p.steps ?? 10)} oninput={(e) => setParam(sel.engine, "steps", Number(e.currentTarget.value))} /></label>
                        <label class="small">noise <input class="input" type="number" min="0" max="0.5" step="0.01" value={String(p.noise_level ?? 0.1)} oninput={(e) => setParam(sel.engine, "noise_level", Number(e.currentTarget.value))} /></label>
                      {/if}
                    </div>
                  </div>
                {/each}
              </div>
            </div>
          {/if}

          <div class="btn-row" style="margin-top:14px">
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

      <div class="card">
        <div class="card__head">
          <h2>{t("player.title")}</h2>
        </div>
        <div class="card__body">
          <AudioPlayer
            tracks={listenTracks}
            inputQuality={single.result?.quality?.input ?? null}
            outputQuality={single.result?.quality?.output ?? null}
            status={single.result?.status ?? null}
          />
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
                <span class="faint">{engineStateLabel(item.status)}</span>
              </div>
            {:else}
              <span class="small faint">{engine.enginesLoading ? t("common.loading") : t("engines.noEngines")}</span>
            {/each}
            <div class="small faint" style="margin-top:6px">{t("protect.chainHint")}</div>
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
              <dt>{t("protect.chain")}</dt>
              <dd class="small">{chain.length}</dd>
            </dl>
          </div>
        </div>
      {/if}
    </div>
  </div>
</div>


