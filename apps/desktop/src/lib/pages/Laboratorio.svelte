<script lang="ts">
  /**
   * Laboratorio di prova (Qwen3-TTS + Whisper).
   *
   * Implementa la UI contro il contratto engine `lab.*`; se il motore non
   * espone ancora i metodi, mostra lo stato reale ("Installabile" + motivo)
   * e consente di preparare file/modelli senza vicoli ciechi.
   */
  import { ui } from "../ui.svelte";
  import { onDestroy, onMount } from "svelte";
  import { hostErrorMessage, t } from "../i18n.svelte";
  import { toast } from "../ui.svelte";
  import {
    ENGINE_EVENTS,
    engineApi,
    labApi,
    onEvent,
    type NlError,
  } from "../ipc";
  import { formatBytes, formatNumber, basename } from "../format";
  import { pickAudioFile, pickDirectory } from "../dialogs";
  import {
    metricLabel,
    normalizeLabOutcome,
    normalizeLabStatus,
    offlineLabStatus,
    pairWavs,
    type LabPair,
    type LabRunOutcome,
    type LabStatusInfo,
  } from "../lab";
  import Badge from "../components/Badge.svelte";
  import Icon from "../components/Icon.svelte";
  import ProgressBar from "../components/ProgressBar.svelte";
  import AudioPlayer from "../components/AudioPlayer.svelte";

  interface ResultEntry {
    id: string;
    pair: LabPair;
    jobId: string;
    status: "completed" | "failed" | "cancelled";
    outcome: LabRunOutcome;
    error?: string | null;
  }

  const LANGUAGES = [
    { id: "it", key: "lab.lang.it" },
    { id: "en", key: "lab.lang.en" },
    { id: "es", key: "lab.lang.es" },
    { id: "fr", key: "lab.lang.fr" },
    { id: "de", key: "lab.lang.de" },
  ];

  /** Qwen3-TTS vuole il nome della lingua; Whisper il codice ISO. */
  const RUN_LANGUAGES: Record<string, string> = {
    it: "Italian",
    en: "English",
    es: "Spanish",
    fr: "French",
    de: "German",
  };

  let status = $state<LabStatusInfo | null>(null);
  let statusLoading = $state(false);
  let statusError = $state<string | null>(null);
  let wavFiles = $state<string[]>([]);
  let resultsDir = $state<string | null>(null);
  let selectedModelId = $state("");
  let qwenLocal = $state("");
  let whisperModel = $state("base");
  let whisperLocal = $state("");
  let deviceId = $state("cpu");
  let language = $state("it");
  let seedText = $state("");
  let text = $state("");
  let refText = $state("");
  let transcribing = $state(false);
  let installBusy = $state<string | null>(null);

  let running = $state(false);
  let cancelling = $state(false);
  let activeJobId = $state<string | null>(null);
  let currentPair = $state<LabPair | null>(null);
  let queue = $state<LabPair[]>([]);
  let progressPct = $state(0);
  let progressMessage = $state("");
  let logs = $state<string[]>([]);
  let results = $state<ResultEntry[]>([]);
  let processedJobs = new Set<string>();

  let pairs = $derived(pairWavs(wavFiles));
  let runnablePairs = $derived(pairs.filter((pair) => pair.protected !== null));
  let models = $derived(status?.models ?? []);
  let whisperModels = $derived(status?.whisper ?? []);
  let devices = $derived(status?.devices ?? []);
  let selectedModel = $derived(models.find((model) => model.id === selectedModelId) ?? null);
  let selectedWhisper = $derived(
    whisperModels.find((model) => model.id === whisperModel) ?? null,
  );
  let backendReady = $derived(Boolean(status?.available));
  let selectedDevice = $derived(devices.find((device) => device.id === deviceId) ?? null);
  let installedBytes = $derived(
    [...models, ...whisperModels]
      .filter((model) => model.installed && model.sizeBytes)
      .reduce((total, model) => total + (model.sizeBytes ?? 0), 0),
  );

  const seedValue = $derived.by(() => {
    const trimmed = seedText.trim();
    if (trimmed === "") return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
  });

  const runBlockReason = $derived.by(() => {
    if (!backendReady) return t("lab.run.notReady");
    if (!selectedModel?.installed) return t("lab.run.needModel");
    if (runnablePairs.length === 0) return t("lab.run.needFiles");
    if (!resultsDir) return t("lab.run.needDir");
    if (!text.trim()) return t("lab.run.needText");
    if (!refText.trim()) return t("lab.run.needRef");
    if (!selectedDevice?.supported) return t("lab.run.needDevice");
    return null;
  });

  function pushLog(message: string): void {
    const stamp = new Date().toLocaleTimeString();
    logs = [...logs.slice(-300), `${stamp} · ${message}`];
  }

  function errorMessage(error: unknown): string {
    return hostErrorMessage(error as NlError);
  }

  async function refreshStatus(showToast = false): Promise<void> {
    statusLoading = true;
    statusError = null;
    try {
      const raw = await labApi.status();
      status = normalizeLabStatus(raw);
      if (!selectedModelId && status.models[0]) selectedModelId = status.models[0].id;
      if (!devices.some((device) => device.id === deviceId)) {
        deviceId = status.devices.find((device) => device.supported)?.id ?? "cpu";
      }
      if (!status.whisper.some((model) => model.id === whisperModel)) {
        whisperModel = status.whisper[0]?.id ?? "base";
      }
      if (showToast) toast("ok", t("lab.common.reload"), t("lab.banner.contract"));
    } catch (error) {
      const message = errorMessage(error);
      status = offlineLabStatus(message);
      statusError = message;
      if (showToast) toast("warn", t("lab.banner.title"), message);
    } finally {
      statusLoading = false;
    }
  }

  async function addWavs(): Promise<void> {
    const picked = await pickAudioFile({ multiple: true, title: t("lab.refs.add") });
    if (!picked || picked.length === 0) return;
    const merged = [...wavFiles];
    for (const file of picked) {
      if (file.toLowerCase().endsWith(".wav") && !merged.includes(file)) merged.push(file);
    }
    wavFiles = merged;
  }

  function removeWav(file: string): void {
    wavFiles = wavFiles.filter((entry) => entry !== file);
  }

  async function chooseResultsDir(): Promise<void> {
    const picked = await pickDirectory(t("lab.refs.dir"));
    if (picked) resultsDir = picked;
  }

  async function waitLabJob(response: unknown, label: string): Promise<void> {
    const payload = (response ?? {}) as Record<string, unknown>;
    const jobId =
      (typeof payload.job_id === "string" && payload.job_id) ||
      (typeof payload.jobId === "string" && payload.jobId) ||
      "";
    if (!jobId) {
      pushLog(`${label}: ok`);
      return;
    }
    pushLog(`${label}: job ${jobId}`);
    const deadline = Date.now() + 1000 * 60 * 120;
    let state = "running";
    while (state !== "completed" && state !== "failed" && state !== "cancelled") {
      if (Date.now() > deadline) throw new Error("timeout");
      await new Promise((resolve) => setTimeout(resolve, 1200));
      const raw = await engineApi.jobStatus(jobId);
      const data = (raw ?? {}) as Record<string, unknown>;
      state = String(data.state ?? data.status ?? "running");
      const progress = (data.progress ?? {}) as Record<string, unknown>;
      const pct = progress.pct;
      if (typeof pct === "number") progressPct = Math.max(0, Math.min(100, pct));
      const message =
        (typeof progress.message === "string" && progress.message) ||
        (typeof data.message === "string" && data.message) ||
        "";
      if (message) {
        progressMessage = message;
        pushLog(`${label}: ${message}`);
      }
      if (state === "failed") {
        const error = data.error;
        const text =
          (error && typeof error === "object" && "message" in error
            ? String((error as Record<string, unknown>).message)
            : typeof error === "string"
              ? error
              : "") || "failed";
        throw new Error(text);
      }
    }
    if (state !== "completed") throw new Error(state);
    pushLog(`${label}: ${state}`);
  }

  async function installQwen(fromLocal: boolean): Promise<void> {
    const model = selectedModel;
    if (!model) return;
    const localPath = fromLocal ? qwenLocal.trim() : "";
    if (fromLocal && localPath === "") {
      toast("warn", t("lab.model.localPath"), t("lab.model.install"));
      return;
    }
    installBusy = model.id;
    try {
      const response = await labApi.installModel({
        id: model.id,
        source: localPath ? "local" : "download",
        localPath: localPath || null,
      });
      await waitLabJob(response, model.label);
      toast("ok", `${model.label} · ${formatBytes(model.sizeBytes ?? 0, 2)}`, t("lab.model.installing"));
      await refreshStatus();
    } catch (error) {
      toast("err", errorMessage(error), model.label);
      pushLog(`${model.label}: ${errorMessage(error)}`);
    } finally {
      installBusy = null;
    }
  }

  async function verifyQwen(): Promise<void> {
    const model = selectedModel;
    if (!model) return;
    installBusy = `${model.id}:verify`;
    try {
      const raw = await labApi.verify(false);
      const payload = (raw ?? {}) as Record<string, unknown>;
      const report = ((payload.lab ?? payload) ?? {}) as Record<string, unknown>;
      await refreshStatus();
      const installed = status?.models.find((entry) => entry.id === model.id)?.installed;
      const ok = Boolean(report.ok) || Boolean(installed);
      const detail = installed
        ? `${model.label} · ${formatBytes(status?.models.find((entry) => entry.id === model.id)?.sizeBytes ?? 0, 2)}`
        : `${t("lab.model.notDownloaded")}. ${t("lab.model.install")} / ${t("lab.model.localPath")}`;
      toast(ok ? "ok" : "warn", detail, t("lab.model.verify"));
    } catch (error) {
      toast("err", t("lab.model.verify"), errorMessage(error));
    } finally {
      installBusy = null;
    }
  }

  async function uninstallQwen(): Promise<void> {
    const model = selectedModel;
    if (!model) return;
    installBusy = model.id;
    try {
      await labApi.uninstallModel(model.id);
      toast("ok", model.label, t("lab.model.uninstall"));
      await refreshStatus();
    } catch (error) {
      toast("err", errorMessage(error), t("lab.model.uninstall"));
    } finally {
      installBusy = null;
    }
  }

  async function installWhisper(fromLocal: boolean): Promise<void> {
    const model = selectedWhisper;
    if (!model) return;
    const localPath = fromLocal ? whisperLocal.trim() : "";
    if (fromLocal && localPath === "") {
      toast("warn", t("lab.whisper.localPath"), t("lab.whisper.install"));
      return;
    }
    installBusy = `whisper:${model.id}`;
    try {
      const response = await labApi.installWhisper({
        model: model.id,
        source: localPath ? "local" : "download",
        localPath: localPath || null,
      });
      await waitLabJob(response, model.label);
      toast("ok", `${model.label} · ${formatBytes(model.sizeBytes ?? 0, 2)}`, t("lab.whisper.install"));
      await refreshStatus();
    } catch (error) {
      toast("err", errorMessage(error), model.label);
      pushLog(`${model.label}: ${errorMessage(error)}`);
    } finally {
      installBusy = null;
    }
  }

  async function uninstallWhisper(): Promise<void> {
    const model = selectedWhisper;
    if (!model) return;
    installBusy = `whisper:${model.id}`;
    try {
      await labApi.uninstallWhisper(model.id);
      toast("ok", model.label, t("lab.whisper.uninstall"));
      await refreshStatus();
    } catch (error) {
      toast("err", errorMessage(error), t("lab.whisper.uninstall"));
    } finally {
      installBusy = null;
    }
  }

  async function transcribeReference(): Promise<void> {
    const pair = runnablePairs[0] ?? pairs[0];
    if (!pair) {
      toast("warn", t("lab.refs.empty"), t("lab.run.transcribe"));
      return;
    }
    transcribing = true;
    try {
      const raw = await labApi.transcribe({
        audio: pair.original,
        model: whisperModel,
        language,
        device: deviceId,
      });
      const payload = (raw ?? {}) as Record<string, unknown>;
      const nested = (payload.result ?? {}) as Record<string, unknown>;
      const transcript =
        (typeof payload.text === "string" && payload.text) ||
        (typeof nested.text === "string" && nested.text) ||
        (typeof payload.transcript === "string" && payload.transcript) ||
        "";
      if (!transcript) {
        toast("warn", t("lab.results.noOutputs"), t("lab.run.transcribe"));
      } else {
        refText = transcript;
        toast("ok", basename(pair.original), t("lab.run.transcribe"));
      }
    } catch (error) {
      toast("err", errorMessage(error), t("lab.run.transcribe"));
    } finally {
      transcribing = false;
    }
  }

  function startRun(): void {
    const reason = runBlockReason;
    if (reason) {
      toast("warn", reason, t("lab.run.title"));
      return;
    }
    queue = [...runnablePairs];
    results = [];
    logs = [];
    processedJobs.clear();
    cancelling = false;
    running = true;
    pushLog(`${t("lab.run.start")} · ${queue.length} × ${selectedModel?.label ?? ""}`);
    void runNext();
  }

  async function runNext(): Promise<void> {
    if (cancelling || queue.length === 0) {
      running = false;
      activeJobId = null;
      currentPair = null;
      progressPct = 0;
      pushLog(cancelling ? t("lab.run.cancel") : `${t("lab.run.title")}: ${t("lab.results.title")}`);
      return;
    }
    const pair = queue.shift();
    if (!pair || !pair.protected) {
      void runNext();
      return;
    }
    currentPair = pair;
    progressPct = 0;
    progressMessage = "";
    try {
      const raw = await labApi.run({
        original: pair.original,
        protected: pair.protected,
        modelId: selectedModelId,
        text: text.trim(),
        refText: refText.trim(),
        language: RUN_LANGUAGES[language] ?? "Italian",
        seed: seedValue,
        device: deviceId,
        outputDir: resultsDir,
      });
      const payload = (raw ?? {}) as Record<string, unknown>;
      const jobId =
        (typeof payload.job_id === "string" && payload.job_id) ||
        (typeof payload.jobId === "string" && payload.jobId) ||
        "";
      if (!jobId) {
        throw new Error(t("lab.results.noOutputs"));
      }
      activeJobId = jobId;
      pushLog(`${basename(pair.original)} → job ${jobId}`);
    } catch (error) {
      const message = errorMessage(error);
      pushLog(`${basename(pair.original)}: ${message}`);
      results = [
        ...results,
        {
          id: pair.id,
          pair,
          jobId: "",
          status: "failed",
          outcome: { outputs: [], raw: null },
          error: message,
        },
      ];
      void runNext();
    }
  }

  function pushProgress(payload: unknown): void {
    if (!activeJobId) return;
    const data = (payload ?? {}) as Record<string, unknown>;
    const jobId = (data.job_id ?? data.jobId) as string | undefined;
    if (jobId && jobId !== activeJobId) return;
    const progress = (data.progress ?? data) as Record<string, unknown>;
    const pct = progress.pct ?? progress.percent ?? data.pct;
    if (typeof pct === "number" && Number.isFinite(pct)) progressPct = Math.max(0, Math.min(100, pct));
    const message = progress.message ?? progress.stage ?? data.message;
    if (typeof message === "string" && message) {
      progressMessage = message;
      pushLog(message);
    }
  }

  function handleFinished(payload: unknown): void {
    const data = (payload ?? {}) as Record<string, unknown>;
    const result = (data.result ?? {}) as Record<string, unknown>;
    const jobId = (data.job_id ?? result.job_id ?? data.jobId) as string | undefined;
    if (!jobId || jobId !== activeJobId || processedJobs.has(jobId)) return;
    processedJobs.add(jobId);
    const pair = currentPair;
    const outcome = normalizeLabOutcome(result);
    results = [
      ...results,
      {
        id: pair?.id ?? jobId,
        pair: pair ?? { id: jobId, original: "", protected: null, originalIsProtected: false },
        jobId,
        status: "completed",
        outcome,
      },
    ];
    pushLog(`${t("lab.results.title")}: ${basename(pair?.original ?? "")}`);
    activeJobId = null;
    currentPair = null;
    void runNext();
  }

  function handleFailed(payload: unknown): void {
    const data = (payload ?? {}) as Record<string, unknown>;
    const jobId = (data.job_id ?? data.jobId) as string | undefined;
    if (!jobId || jobId !== activeJobId || processedJobs.has(jobId)) return;
    processedJobs.add(jobId);
    const message =
      (typeof data.error === "string" && data.error) ||
      (typeof data.message === "string" && data.message) ||
      t("status.failed");
    const pair = currentPair;
    results = [
      ...results,
      {
        id: pair?.id ?? jobId,
        pair: pair ?? { id: jobId, original: "", protected: null, originalIsProtected: false },
        jobId,
        status: "failed",
        outcome: { outputs: [], raw: data },
        error: message,
      },
    ];
    pushLog(`${basename(pair?.original ?? "")}: ${message}`);
    activeJobId = null;
    currentPair = null;
    void runNext();
  }

  async function pollJob(jobId: string): Promise<void> {
    try {
      const raw = await engineApi.jobStatus(jobId);
      const data = (raw ?? {}) as Record<string, unknown>;
      const state = (data.state ?? data.status ?? "") as string;
      if (state === "completed" && !processedJobs.has(jobId)) {
        handleFinished({ job_id: jobId, result: data.result ?? data });
      } else if (state === "failed" && !processedJobs.has(jobId)) {
        handleFailed({ job_id: jobId, error: data.error });
      } else if (state === "cancelled" && !processedJobs.has(jobId)) {
        processedJobs.add(jobId);
        pushLog(t("lab.run.cancel"));
        activeJobId = null;
        currentPair = null;
        running = false;
      }
    } catch {
      /* il polling è solo un fallback */
    }
  }

  async function cancelRun(): Promise<void> {
    cancelling = true;
    pushLog(t("lab.run.cancel"));
    const jobId = activeJobId;
    if (jobId) {
      try {
        await engineApi.cancelJob(jobId);
      } catch (error) {
        pushLog(errorMessage(error));
      }
    }
  }

  function openFolder(path: string | null): void {
    if (path) void engineApi.openPath(path, "folder");
  }

  function outputLabel(output: { label?: string | null; path: string | null }): string {
    const key = output.label ?? "";
    if (key === "original_clone") return t("lab.results.cloneOriginal");
    if (key === "protected_clone") return t("lab.results.cloneProtected");
    return output.label ?? basename(output.path);
  }

  function resultTracks(entry: ResultEntry): { path: string; label: string }[] {
    const tracks: { path: string; label: string }[] = [];
    if (entry.pair.original) {
      tracks.push({ path: entry.pair.original, label: t("lab.results.referenceOriginal") });
    }
    if (entry.pair.protected) {
      tracks.push({ path: entry.pair.protected, label: t("lab.results.referenceProtected") });
    }
    for (const output of entry.outcome.outputs) {
      if (output.path) {
        tracks.push({ path: output.path, label: outputLabel(output) });
      }
    }
    return tracks;
  }

  function metricRows(entry: ResultEntry): [string, string][] {
    const rows: [string, string][] = [];
    for (const output of entry.outcome.outputs) {
      if (!output.metrics) continue;
      const label = outputLabel(output);
      for (const [key, value] of Object.entries(output.metrics)) {
        if (typeof value === "number" && Number.isFinite(value)) {
          rows.push([`${label} · ${metricLabel(key)}`, formatNumber(value, 2)]);
        } else if (typeof value === "string" && value.length > 0) {
          rows.push([`${label} · ${metricLabel(key)}`, value]);
        }
      }
    }
    return rows.slice(0, 20);
  }

  let unlisten: Array<() => void> = [];
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    void refreshStatus();
    void (async () => {
      unlisten = [
        await onEvent(ENGINE_EVENTS.progress, (payload) => pushProgress(payload)),
        await onEvent(ENGINE_EVENTS.finished, (payload) => handleFinished(payload)),
        await onEvent(ENGINE_EVENTS.failed, (payload) => handleFailed(payload)),
        await onEvent(ENGINE_EVENTS.labProgress, (payload) => pushProgress(payload)),
        await onEvent(ENGINE_EVENTS.labFinished, (payload) => handleFinished(payload)),
        await onEvent(ENGINE_EVENTS.labFailed, (payload) => handleFailed(payload)),
      ];
    })();
    pollTimer = setInterval(() => {
      if (activeJobId) void pollJob(activeJobId);
    }, 1500);
  });

  onDestroy(() => {
    for (const off of unlisten) off();
    if (pollTimer) clearInterval(pollTimer);
  });
</script>

<div class="page mode-{ui.viewMode}">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("lab.title")}</h1>
      <p>{t("lab.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn" onclick={() => refreshStatus(true)} disabled={statusLoading}>
        <Icon name="refresh" size={14} />
        {statusLoading ? t("lab.banner.retry") : t("lab.common.reload")}
      </button>
    </div>
  </div>

  {#if !backendReady}
    <div class="warn-box">
      <div class="warn-box__title">{t("lab.banner.title")}</div>
      <div>{t("lab.banner.body")}</div>
      {#if statusError}
        <div class="small mono" style="margin-top:6px">{statusError}</div>
      {/if}
      <div class="row" style="margin-top:8px">
        <span class="chip">{t("lab.banner.contract")}</span>
        <button class="btn btn--sm" onclick={() => refreshStatus(true)}>{t("lab.banner.retry")}</button>
      </div>
    </div>
  {/if}

  <div class="split">
    <div class="col">
      <section class="card">
        <div class="card__head">
          <h2>{t("lab.refs.title")}</h2>
          <span class="spacer"></span>
          <span class="badge badge--muted">{pairs.length}</span>
          <button class="btn btn--sm" onclick={addWavs}>
            <Icon name="plus" size={13} />{t("lab.refs.add")}
          </button>
          {#if wavFiles.length > 0}
            <button class="btn btn--sm btn--ghost" onclick={() => (wavFiles = [])}>
              {t("lab.refs.clear")}
            </button>
          {/if}
        </div>
        <div class="card__body">
          <p class="small muted mt-0">{t("lab.refs.hint")}</p>
          {#if pairs.length === 0}
            <div class="empty">{t("lab.refs.empty")}</div>
          {:else}
            <div class="col gap-sm">
              {#each pairs as pair (pair.id)}
                <div class="list-row">
                  <span class="dot {pair.protected ? 'dot--ok' : 'dot--warn'}"></span>
                  <div class="list-row__main">
                    <div class="truncate small" title={pair.original}>{basename(pair.original)}</div>
                    {#if pair.protected}
                      <div class="truncate faint mono">{basename(pair.protected)}</div>
                    {:else}
                      <div class="small faint">
                        {pair.originalIsProtected
                          ? t("lab.refs.pairMissing")
                          : t("lab.refs.pairMissing")}
                      </div>
                    {/if}
                  </div>
                  <Badge
                    label={pair.protected ? t("lab.refs.pairOk") : t("lab.refs.pairMissing")}
                    tone={pair.protected ? "ok" : "warn"}
                  />
                  <button
                    class="btn btn--sm btn--ghost"
                    onclick={() => removeWav(pair.original)}
                    title={t("lab.refs.remove")}
                    aria-label={t("lab.refs.remove")}
                  >
                    <Icon name="trash" size={14} />
                  </button>
                </div>
              {/each}
            </div>
          {/if}
          <div class="row" style="margin-top:10px">
            <div class="field" style="flex:1 1 260px">
              <span class="field__label">{t("lab.refs.dir")}</span>
              <div class="input-row">
                <input class="input" readonly value={resultsDir ?? ""} placeholder={t("lab.refs.dirEmpty")} />
                <button class="btn btn--sm" onclick={chooseResultsDir}>{t("lab.refs.pickDir")}</button>
              </div>
              <span class="field__help">{t("lab.refs.dirHint")}</span>
            </div>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card__head">
          <h2>{t("lab.run.title")}</h2>
          <span class="spacer"></span>
          {#if selectedModel}
            <span class="chip">{selectedModel.label}</span>
          {/if}
          {#if selectedDevice}
            <span class="chip">{selectedDevice.label}</span>
          {/if}
        </div>
        <div class="card__body col">
          <div class="field">
            <label for="lab-text">{t("lab.run.text")}</label>
            <textarea
              id="lab-text"
              class="textarea"
              rows="3"
              bind:value={text}
              placeholder={t("lab.run.textPlaceholder")}
            ></textarea>
          </div>

          <div class="row gap-lg">
            <div class="field" style="flex:1 1 220px">
              <label for="lab-reftext">{t("lab.run.refText")}</label>
              <textarea
                id="lab-reftext"
                class="textarea"
                rows="3"
                bind:value={refText}
                placeholder={t("lab.run.refTextPlaceholder")}
              ></textarea>
            </div>
            <div class="field" style="flex:0 1 220px">
              <span class="field__label">{t("lab.run.transcribe")}</span>
              <select class="select" bind:value={whisperModel}>
                {#each whisperModels as model (model.id)}
                  <option value={model.id}>
                    {model.label}{model.installed ? " ✓" : ""}
                  </option>
                {/each}
              </select>
              <button
                class="btn btn--sm"
                onclick={transcribeReference}
                disabled={transcribing || !backendReady}
                title={!backendReady ? t("lab.run.notReady") : t("lab.run.transcribe")}
              >
                {transcribing ? t("lab.run.transcribing") : t("lab.run.transcribe")}
              </button>
              <span class="field__help">{t("lab.whisper.hint")}</span>
            </div>
          </div>

          <div class="row gap-lg">
            <div class="field" style="flex:0 0 120px">
              <label for="lab-seed">{t("lab.run.seed")}</label>
              <input
                id="lab-seed"
                class="input"
                type="number"
                step="1"
                bind:value={seedText}
                placeholder="—"
              />
              <span class="field__help">{t("lab.run.seedHint")}</span>
            </div>
            <div class="field" style="flex:0 0 160px">
              <label for="lab-language">{t("lab.run.language")}</label>
              <select id="lab-language" class="select" bind:value={language}>
                {#each LANGUAGES as lang (lang.id)}
                  <option value={lang.id}>{t(lang.key)}</option>
                {/each}
              </select>
            </div>
            <div class="field" style="flex:1 1 200px">
              <span class="field__label">{t("lab.run.whisper")}</span>
              <div class="row">
                {#each whisperModels as model (model.id)}
                  <span class="chip {model.installed ? 'chip--ok' : ''}">
                    {model.params ?? model.id} · {formatBytes(model.sizeBytes, 1)}
                  </span>
                {/each}
              </div>
            </div>
          </div>

          <div class="row">
            <button class="btn btn--primary" onclick={startRun} disabled={running || Boolean(runBlockReason)} title={runBlockReason ?? t("lab.run.start")}>
              <Icon name="play" size={14} />
              {running ? t("lab.run.running") : t("lab.run.start")}
            </button>
            {#if running}
              <button class="btn btn--danger" onclick={cancelRun} disabled={cancelling}>
                {cancelling ? t("lab.run.cancel") : t("lab.run.cancel")}
              </button>
            {/if}
            {#if runBlockReason}
              <span class="small faint">{runBlockReason}</span>
            {/if}
          </div>

          {#if running || progressPct > 0 || progressMessage}
            <div class="col gap-sm">
              <div class="row">
                <ProgressBar value={progressPct} />
                <span class="small tnum muted">{Math.round(progressPct)}%</span>
                {#if queue.length > 0}
                  <span class="small faint">{t("lab.run.queue", { n: queue.length })}</span>
                {/if}
              </div>
              {#if progressMessage}
                <span class="small muted">{progressMessage}</span>
              {/if}
            </div>
          {/if}
        </div>
      </section>

      <section class="card advanced-only">
        <div class="card__head">
          <h2>{t("lab.log.title")}</h2>
          <span class="spacer"></span>
          <span class="small faint">{logs.length}</span>
        </div>
        <div class="card__body card__body--flush">
          {#if logs.length === 0}
            <div class="empty">{t("lab.log.empty")}</div>
          {:else}
            <div class="lab-log mono">
              {#each logs as line}
                <div>{line}</div>
              {/each}
            </div>
          {/if}
        </div>
      </section>

      <section class="card">
        <div class="card__head">
          <h2>{t("lab.results.title")}</h2>
          <span class="spacer"></span>
          {#if resultsDir}
            <button class="btn btn--sm btn--ghost" onclick={() => openFolder(resultsDir)}>
              {t("lab.results.openFolder")}
            </button>
          {/if}
        </div>
        <div class="card__body">
          {#if results.length === 0}
            <div class="empty">{t("lab.results.empty")}</div>
          {:else}
            <div class="col gap-lg">
              {#each results as entry (entry.id + entry.jobId)}
                <div class="col gap-sm" style="border-bottom:1px solid var(--nl-border); padding-bottom:10px">
                  <div class="row">
                    <span class="dot {entry.status === 'completed' ? 'dot--ok' : 'dot--err'}"></span>
                    <span class="bold small truncate">{basename(entry.pair.original)}</span>
                    <Badge
                      label={entry.status === "completed" ? "OK" : t("status.failed")}
                      tone={entry.status === "completed" ? "ok" : "err"}
                    />
                    {#if entry.jobId}
                      <span class="chip">{entry.jobId.slice(0, 8)}</span>
                    {/if}
                  </div>
                  {#if entry.error}
                    <div class="err-box small">{entry.error}</div>
                  {/if}
                  {#if resultTracks(entry).length > 1}
                    <AudioPlayer tracks={resultTracks(entry)} status={entry.status === "completed" ? "protected" : "failed"} />
                  {:else if entry.status === "completed"}
                    <div class="small faint">{t("lab.results.noOutputs")}</div>
                  {/if}
                  {#if metricRows(entry).length > 0}
                    <div>
                      <div class="field__label">{t("lab.results.metrics")}</div>
                      <table class="table table--dense tnum">
                        <tbody>
                          {#each metricRows(entry) as [key, value]}
                            <tr>
                              <td class="muted small">{key}</td>
                              <td class="mono small">{value}</td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                  {/if}
                  <div class="col gap-sm">
                    {#each entry.outcome.outputs.filter((output) => output.speakerSimilarity != null) as output}
                      <div class="row small">
                        <span class="muted">{t("lab.results.similarity")} · {outputLabel(output)}:</span>
                        <span class="mono">
                          {formatNumber((output.speakerSimilarity ?? 0) * 100, 1)}%
                        </span>
                      </div>
                    {/each}
                    {#if entry.outcome.speakerSimilarity != null}
                      <div class="row small">
                        <span class="muted">{t("lab.results.similarityDelta")}:</span>
                        <span class="mono">{formatNumber(entry.outcome.speakerSimilarity * 100, 2)}%</span>
                      </div>
                    {/if}
                    {#if !entry.outcome.outputs.some((output) => output.speakerSimilarity != null) && entry.outcome.speakerSimilarity == null}
                      <div class="row small">
                        <span class="muted">{t("lab.results.similarity")}:</span>
                        <span class="faint">{t("lab.results.similarityMissing")}</span>
                      </div>
                    {/if}
                  </div>
                  <details>
                    <summary class="small faint">{t("lab.results.raw")}</summary>
                    <pre class="lab-raw mono">{JSON.stringify(entry.outcome.raw, null, 2)}</pre>
                  </details>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      </section>
    </div>

    <div class="col">
      <section class="card expert-only">
        <div class="card__head">
          <h2>{t("lab.model.title")}</h2>
          <span class="spacer"></span>
          {#if selectedModel}
            <Badge
              label={selectedModel.installed ? t("lab.model.installed") : t("lab.model.installable")}
              tone={selectedModel.installed ? "ok" : "warn"}
            />
          {/if}
        </div>
        <div class="card__body col">
          {#each models as model (model.id)}
            <label class="lab-model-option {selectedModelId === model.id ? 'is-selected' : ''}">
              <input
                type="radio"
                name="lab-model"
                value={model.id}
                checked={selectedModelId === model.id}
                onchange={() => (selectedModelId = model.id)}
              />
              <span class="lab-model-option__text">
                <span class="bold small">{model.label}</span>
                <span class="small faint tnum">
                  {t("lab.model.size")}: {formatBytes(model.sizeBytes, 1)}
                </span>
              </span>
              <Badge
                label={model.installed ? t("lab.model.installed") : t("lab.model.installable")}
                tone={model.installed ? "ok" : "muted"}
              />
            </label>
          {/each}
          {#if models.some((model) => model.estimated)}
            <div class="small faint">{t("lab.model.estimated")}</div>
          {/if}
          {#if selectedModel?.path}
            <div class="small faint mono truncate" title={selectedModel.path}>{selectedModel.path}</div>
          {/if}
          {#if selectedModel?.reason && !selectedModel.installed}
            <div class="hint-box small">
              {t("lab.model.reason")}:
              <span class="mono">
                {selectedModel.reason?.startsWith("missing:")
                  ? t("lab.model.notDownloaded")
                  : selectedModel.reason}
              </span>
            </div>
          {/if}
          <div class="field">
            <label for="lab-qwen-local">{t("lab.model.localPath")}</label>
            <input
              id="lab-qwen-local"
              class="input"
              bind:value={qwenLocal}
              placeholder={t("lab.model.localPathPlaceholder")}
            />
          </div>
          <div class="btn-row">
            <button
              class="btn btn--sm"
              onclick={() => installQwen(false)}
              disabled={installBusy === selectedModel?.id}
            >
              {installBusy === selectedModel?.id ? t("lab.model.installing") : t("lab.model.install")}
            </button>
            <button
              class="btn btn--sm"
              onclick={() => installQwen(true)}
              disabled={installBusy === selectedModel?.id}
            >
              {t("lab.model.sourceLocal")}
            </button>
            <button class="btn btn--sm" onclick={verifyQwen} disabled={installBusy === `${selectedModel?.id}:verify`}>
              {t("lab.model.verify")}
            </button>
            <button
              class="btn btn--sm btn--danger"
              onclick={uninstallQwen}
              disabled={!selectedModel?.installed || installBusy === selectedModel?.id}
            >
              {t("lab.model.uninstall")}
            </button>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card__head">
          <h2>{t("lab.whisper.title")}</h2>
          <span class="spacer"></span>
          {#if selectedWhisper}
            <Badge
              label={selectedWhisper.installed ? t("lab.whisper.installed") : t("lab.whisper.installable")}
              tone={selectedWhisper.installed ? "ok" : "muted"}
            />
          {/if}
        </div>
        <div class="card__body col">
          <div class="field">
            <label for="lab-whisper">{t("lab.run.whisper")}</label>
            <select id="lab-whisper" class="select" bind:value={whisperModel}>
              {#each whisperModels as model (model.id)}
                <option value={model.id}>
                  {model.label} · {formatBytes(model.sizeBytes, 1)}{model.installed ? " ✓" : ""}
                </option>
              {/each}
            </select>
          </div>
          <div class="field">
            <label for="lab-whisper-local">{t("lab.whisper.localPath")}</label>
            <input
              id="lab-whisper-local"
              class="input"
              bind:value={whisperLocal}
              placeholder={t("lab.model.localPathPlaceholder")}
            />
          </div>
          <div class="btn-row">
            <button
              class="btn btn--sm"
              onclick={() => installWhisper(false)}
              disabled={installBusy === `whisper:${selectedWhisper?.id}`}
            >
              {installBusy === `whisper:${selectedWhisper?.id}` ? t("lab.model.installing") : t("lab.whisper.install")}
            </button>
            <button
              class="btn btn--sm"
              onclick={() => installWhisper(true)}
              disabled={installBusy === `whisper:${selectedWhisper?.id}`}
            >
              {t("lab.model.sourceLocal")}
            </button>
            <button
              class="btn btn--sm btn--danger"
              onclick={uninstallWhisper}
              disabled={!selectedWhisper?.installed}
            >
              {t("lab.whisper.uninstall")}
            </button>
          </div>
          <div class="small faint">{t("lab.whisper.hint")}</div>
        </div>
      </section>

      <section class="card">
        <div class="card__head"><h2>{t("lab.device.title")}</h2></div>
        <div class="card__body col">
          {#if devices.length === 0}
            <span class="small faint">{t("lab.space.unknown")}</span>
          {:else}
            {#each devices as device (device.id)}
              <label class="check {device.supported ? '' : 'check--disabled'}">
                <input
                  type="radio"
                  name="lab-device"
                  value={device.id}
                  checked={deviceId === device.id}
                  disabled={!device.supported}
                  onchange={() => (deviceId = device.id)}
                />
                <span>
                  <span class="small bold">{device.label}</span>
                  {#if device.supported}
                    {#if deviceId === device.id}
                      <span class="chip" style="margin-left:6px">{t("lab.device.selected")}</span>
                    {/if}
                    {#if device.detail && deviceId === device.id}
                      <div class="small faint">{device.detail}</div>
                    {/if}
                  {:else}
                    <div class="small faint">{t("lab.device.unsupported", { reason: device.id === "cuda" ? "CUDA" : "—" })}</div>
                  {/if}
                </span>
              </label>
            {/each}
          {/if}
        </div>
      </section>

      <section class="card">
        <div class="card__head">
          <h2>{t("lab.space.title")}</h2>
          <span class="spacer"></span>
          {#if status?.modelsDir}
            <button class="btn btn--sm btn--ghost" onclick={() => openFolder(status?.modelsDir ?? null)}>
              {t("lab.results.openFolder")}
            </button>
          {/if}
        </div>
        <div class="card__body col">
          <dl class="kv">
            <dt>{t("lab.space.runtime")}</dt>
            <dd class="mono">{status?.runtimeBytes != null ? formatBytes(status.runtimeBytes, 2) : t("lab.space.unknown")}</dd>
            <dt>{t("lab.space.models")}</dt>
            <dd class="mono">{formatBytes(installedBytes, 2)}</dd>
            <dt>{t("lab.space.free")}</dt>
            <dd class="mono">{status?.freeBytes != null ? formatBytes(status.freeBytes, 2) : t("lab.space.unknown")}</dd>
            {#if status?.dataDir}
              <dt>{t("lab.space.dataDir")}</dt>
              <dd class="mono small truncate" title={status.dataDir}>{status.dataDir}</dd>
            {/if}
          </dl>
          <div class="small faint">{t("lab.space.uninstallHint")}</div>
          <div class="col gap-sm">
            {#each [...models, ...whisperModels].filter((model) => model.installed) as model (model.id)}
              <div class="list-row">
                <div class="list-row__main">
                  <span class="small">{model.label}</span>
                  <span class="small faint tnum"> · {formatBytes(model.sizeBytes, 1)}</span>
                </div>
                <button
                  class="btn btn--sm btn--ghost"
                  onclick={() =>
                    model.kind === "qwen"
                      ? (selectedModelId = model.id, uninstallQwen())
                      : (whisperModel = model.id, uninstallWhisper())}
                >
                  {t("lab.model.uninstall")}
                </button>
              </div>
            {/each}
          </div>
        </div>
      </section>
    </div>
  </div>

  <div class="hint-box small">{t("lab.results.note")}</div>
</div>

<style>
  .lab-log {
    max-height: 220px;
    overflow: auto;
    padding: 8px 14px;
    font-size: 11px;
    line-height: 1.55;
    color: var(--nl-text-muted);
  }
  .lab-log > div:nth-child(odd) {
    color: var(--nl-text-faint);
  }
  .lab-raw {
    max-height: 260px;
    overflow: auto;
    padding: 8px;
    background: var(--nl-bg-2);
    border: 1px solid var(--nl-border);
    border-radius: var(--nl-radius-sm);
    font-size: 10.5px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .lab-model-option {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    border: 1px solid var(--nl-border);
    border-radius: var(--nl-radius-sm);
    cursor: pointer;
    background: var(--nl-bg-2);
  }
  .lab-model-option.is-selected {
    border-color: var(--nl-focus);
    background: var(--nl-surface-2);
  }
  .lab-model-option__text {
    display: flex;
    flex-direction: column;
    gap: 1px;
    flex: 1 1 auto;
    min-width: 0;
  }
</style>
