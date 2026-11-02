/** Stato dei lavori (singolo e batch) alimentato dagli eventi del motore. */
import { ENGINE_EVENTS, engineApi, onEvent, toNlError, type NlError } from "./ipc";
import type { ChainStep, JobResult, ProgressEvent } from "./types";
import { ui, toast } from "./ui.svelte";
import { t } from "./i18n.svelte";

export type StepPhase = "pending" | "running" | "done" | "failed" | "skipped";

export interface StepState {
  index: number;
  engine: string;
  action: string;
  required: boolean;
  phase: StepPhase;
  pct: number;
  message: string;
}

export type BatchItemStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface BatchItem {
  path: string;
  status: BatchItemStatus;
  pct: number;
  error?: string | null;
  result?: JobResult | null;
}

function emptySteps(chain: ChainStep[]): StepState[] {
  return chain.map((step, index) => ({
    index,
    engine: step.engine,
    action: step.action,
    required: step.required,
    phase: "pending",
    pct: 0,
    message: "",
  }));
}

export const single = $state({
  jobId: null as string | null,
  inputPath: null as string | null,
  outputDir: null as string | null,
  chain: [] as ChainStep[],
  steps: [] as StepState[],
  running: false,
  cancelling: false,
  pct: 0,
  message: "",
  startedAt: 0,
  finishedAt: 0,
  result: null as JobResult | null,
  error: null as NlError | null,
});

export const batch = $state({
  jobId: null as string | null,
  outputDir: null as string | null,
  chain: [] as ChainStep[],
  items: [] as BatchItem[],
  running: false,
  cancelling: false,
  pct: 0,
  message: "",
  startedAt: 0,
  result: null as JobResult | null,
  error: null as NlError | null,
});

export const history = $state<{ items: JobResult[] }>({ items: [] });

let pollTimer: number | null = null;

function stopPoll(): void {
  if (pollTimer != null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPoll(): void {
  if (pollTimer != null) return;
  pollTimer = window.setInterval(() => {
    void pollOnce();
  }, 2500);
}

function pushHistory(result: JobResult): void {
  if (!result?.job_id) return;
  const exists = history.items.some((item) => item.job_id === result.job_id);
  if (exists) return;
  history.items = [result, ...history.items].slice(0, 40);
}

function clampPct(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

/** Indice del passo tollerando step_index 0-based o 1-based. */
function stepIndexFor(value: number | null | undefined, total: number): number {
  if (value == null || !Number.isFinite(value)) return -1;
  const raw = Math.round(value as number);
  if (raw >= 0 && raw < total) return raw;
  if (raw >= 1 && raw - 1 < total) return raw - 1;
  return -1;
}

function updateSingleSteps(ev: ProgressEvent): void {
  if (single.steps.length === 0) return;
  const total = single.steps.length;
  let index = stepIndexFor(ev.step_index, total);
  if (index < 0 && ev.step) {
    const needle = String(ev.step);
    const found = single.steps.findIndex(
      (step) =>
        needle.includes(step.engine) ||
        needle.includes(step.action) ||
        step.engine.includes(needle),
    );
    if (found >= 0) index = found;
  }
  if (index < 0) return;

  for (let i = 0; i < total; i += 1) {
    const step = single.steps[i];
    if (i < index && step.phase !== "failed") step.phase = "done";
  }
  const current = single.steps[index];
  if (current.phase !== "failed") current.phase = "running";
  if (ev.message) current.message = String(ev.message);
  if (typeof ev.pct === "number") current.pct = clampPct(ev.pct);

  const stepPct = typeof ev.pct === "number" ? clampPct(ev.pct) : 0;
  single.pct = Math.min(99, Math.round(((index + stepPct / 100) / total) * 100));
  single.message = ev.message ? String(ev.message) : current.message;
}

function markStepsFinished(status: string): void {
  const finished = status === "protected" || status === "partial";
  for (const step of single.steps) {
    if (step.phase === "running") step.phase = finished ? "done" : "failed";
    else if (step.phase === "pending" && finished) step.phase = "skipped";
  }
}

export function finalizeSingle(result: JobResult): void {
  if (single.jobId && result.job_id && result.job_id !== single.jobId) return;
  if (!single.running && single.result) return;
  const status = result.status ?? "unknown";
  single.result = result;
  single.running = false;
  single.cancelling = false;
  single.finishedAt = Date.now();
  single.pct = status === "cancelled" ? single.pct : 100;
  markStepsFinished(status);
  pushHistory(result);
  if (status === "protected") toast("ok", t("protect.jobFinished"), t("status.protected"));
  else if (status === "cancelled") toast("info", t("protect.jobCancelled"));
  else if (status === "partial") toast("warn", t("protect.jobFinished"), t("status.partial"));
  else toast("err", t("protect.jobFailed"), t("status.failed"));
  if (!batch.running) stopPoll();
}

function finalizeBatch(result: JobResult): void {
  if (batch.jobId && result.job_id && result.job_id !== batch.jobId) return;
  if (!batch.running && batch.result) return;
  batch.result = result;
  batch.running = false;
  batch.cancelling = false;
  batch.pct = 100;

  const rawItems = (result as Record<string, unknown>).items;
  if (Array.isArray(rawItems)) {
    for (const raw of rawItems) {
      if (!raw || typeof raw !== "object") continue;
      const item = raw as Record<string, unknown>;
      const path =
        (typeof item.path === "string" && item.path) ||
        (typeof item.input_path === "string" && item.input_path) ||
        (typeof item.input === "string" && item.input) ||
        "";
      if (!path) continue;
      const target = batch.items.find((entry) => entry.path === path);
      if (!target) continue;
      const status = typeof item.status === "string" ? item.status : "unknown";
      target.status =
        status === "protected" || status === "partial" || status === "done"
          ? "done"
          : status === "cancelled"
            ? "cancelled"
            : "failed";
      target.pct = 100;
      target.result = item as unknown as JobResult;
      if (status === "failed") target.error = typeof item.error === "string" ? item.error : "failed";
    }
  } else {
    const fallback: BatchItemStatus =
      result.status === "protected" || result.status === "partial" ? "done" : "failed";
    for (const item of batch.items) {
      if (item.status === "done" || item.status === "failed" || item.status === "cancelled") continue;
      item.status = fallback;
      item.pct = 100;
    }
  }

  const failed = batch.items.filter((entry) => entry.status === "failed").length;
  if (failed > 0) toast("warn", t("batch.batchResult"), t("batch.failed"));
  else toast("ok", t("batch.batchResult"));
  pushHistory(result);
  if (!single.running) stopPoll();
}

function finalizeFailure(jobId: string, result: JobResult | null, error: NlError | null): void {
  const payload: JobResult = result ?? {
    job_id: jobId,
    status: "failed",
    error: error?.message ?? "engine failure",
  };
  if (single.jobId === jobId) {
    single.error = error;
    finalizeSingle(payload);
  } else if (batch.jobId === jobId) {
    batch.error = error;
    finalizeBatch(payload);
  }
}

async function pollOnce(): Promise<void> {
  const singleRunning = single.running && single.jobId;
  const batchRunning = batch.running && batch.jobId;
  if (!singleRunning && !batchRunning) {
    stopPoll();
    return;
  }
  if (singleRunning && single.jobId) {
    try {
      const status = await engineApi.jobStatus(single.jobId);
      if (status.result) finalizeSingle(status.result);
      else if (typeof status.progress === "number") {
        single.pct = Math.max(single.pct, clampPct(status.progress));
      }
      if (status.error) {
        finalizeFailure(single.jobId, null, {
          name: "NlError",
          code: "ENGINE_RPC_ERROR",
          message: status.error,
        } as unknown as NlError);
      }
    } catch {
      /* il polling è un fallback: gli eventi restano la sorgente primaria */
    }
  }
  if (batchRunning && batch.jobId) {
    try {
      const status = await engineApi.jobStatus(batch.jobId);
      if (status.result) finalizeBatch(status.result);
      else if (typeof status.progress === "number") {
        batch.pct = Math.max(batch.pct, clampPct(status.progress));
      }
    } catch {
      /* idem */
    }
  }
}

export async function startSingleJob(options: {
  inputPath: string;
  outputDir: string;
  chain: ChainStep[];
  options?: Record<string, unknown>;
  preset?: string;
}): Promise<boolean> {
  single.jobId = null;
  single.inputPath = options.inputPath;
  single.outputDir = options.outputDir;
  single.chain = options.chain;
  single.steps = emptySteps(options.chain);
  single.result = null;
  single.error = null;
  single.running = true;
  single.cancelling = false;
  single.pct = 0;
  single.message = "";
  single.startedAt = Date.now();
  single.finishedAt = 0;
  try {
    const response = await engineApi.startJob({
      inputPath: options.inputPath,
      outputDir: options.outputDir,
      chain: options.chain,
      options: options.options,
      preset: options.preset,
    });
    single.jobId = response.job_id;
    startPoll();
    toast("info", t("protect.jobStarted"), response.job_id);
    return true;
  } catch (err) {
    single.running = false;
    single.error = toNlError(err);
    toast("err", t("protect.jobFailed"), single.error.message);
    return false;
  }
}

export async function cancelSingleJob(): Promise<void> {
  if (!single.jobId || !single.running) return;
  single.cancelling = true;
  try {
    await engineApi.cancelJob(single.jobId);
    toast("info", t("protect.jobCancelled"));
  } catch (err) {
    single.error = toNlError(err);
    toast("err", t("common.error"), single.error.message);
  } finally {
    single.cancelling = false;
  }
}

export async function startBatchJob(options: {
  inputs: string[];
  outputDir: string;
  chain: ChainStep[];
  options?: Record<string, unknown>;
  preset?: string;
}): Promise<boolean> {
  batch.jobId = null;
  batch.outputDir = options.outputDir;
  batch.chain = options.chain;
  batch.items = options.inputs.map((path) => ({ path, status: "queued", pct: 0 }));
  batch.result = null;
  batch.error = null;
  batch.running = true;
  batch.cancelling = false;
  batch.pct = 0;
  batch.message = "";
  batch.startedAt = Date.now();
  try {
    const response = await engineApi.startBatch({
      inputs: options.inputs,
      outputDir: options.outputDir,
      chain: options.chain,
      options: options.options,
      preset: options.preset,
    });
    batch.jobId = response.job_id;
    startPoll();
    toast("info", t("batch.batchStarted"), response.job_id);
    return true;
  } catch (err) {
    batch.running = false;
    batch.error = toNlError(err);
    toast("err", t("batch.batchFailed"), batch.error.message);
    return false;
  }
}

export async function cancelBatchJob(): Promise<void> {
  if (!batch.jobId || !batch.running) return;
  batch.cancelling = true;
  try {
    await engineApi.cancelJob(batch.jobId);
    toast("info", t("batch.batchStopped"));
  } catch (err) {
    batch.error = toNlError(err);
    toast("err", t("common.error"), batch.error.message);
  } finally {
    batch.cancelling = false;
  }
}

/* --------------------------- eventi del motore ---------------------------- */

function handleProgress(ev: ProgressEvent): void {
  if (!ev || typeof ev.job_id !== "string") return;
  if (ev.job_id === single.jobId) {
    updateSingleSteps(ev);
    if (!single.message && ev.message) single.message = String(ev.message);
    return;
  }
  if (ev.job_id === batch.jobId) {
    if (typeof ev.pct === "number") batch.pct = Math.max(batch.pct, clampPct(ev.pct));
    if (ev.message) batch.message = String(ev.message);
    const rawIndex = ev.file_index ?? ev.index ?? ev.item_index;
    const index = typeof rawIndex === "number" ? rawIndex : -1;
    if (index >= 0 && index < batch.items.length) {
      for (let i = 0; i < index; i += 1) {
        if (batch.items[i].status === "queued") {
          batch.items[i].status = "done";
          batch.items[i].pct = 100;
        }
      }
      batch.items[index].status = "running";
      if (typeof ev.pct === "number") batch.items[index].pct = clampPct(ev.pct);
    }
  }
}

function handleFinished(payload: { job_id?: string; result?: JobResult }): void {
  const result = payload?.result;
  if (!result) return;
  const jobId = result.job_id || payload.job_id;
  if (single.jobId && jobId === single.jobId) finalizeSingle(result);
  else if (batch.jobId && jobId === batch.jobId) finalizeBatch(result);
}

function handleFailed(payload: { job_id?: string; result?: JobResult; error?: string }): void {
  const result = payload?.result;
  if (result) {
    handleFinished({ job_id: payload.job_id, result });
    return;
  }
  const jobId = payload?.job_id;
  if (!jobId) return;
  const error = { name: "NlError", code: "ENGINE_RPC_ERROR", message: payload.error ?? "job failed" } as NlError;
  finalizeFailure(jobId, null, error);
}

let unlistenJobs: (() => void)[] = [];

/** Registra i listener degli eventi job; idempotente. */
export async function attachJobListeners(): Promise<void> {
  if (unlistenJobs.length > 0) return;
  const unwatch = await Promise.all([
    onEvent<ProgressEvent>(ENGINE_EVENTS.progress, handleProgress),
    onEvent<{ job_id?: string; result?: JobResult }>(ENGINE_EVENTS.finished, handleFinished),
    onEvent<{ job_id?: string; result?: JobResult; error?: string }>(ENGINE_EVENTS.failed, handleFailed),
  ]);
  unlistenJobs = unwatch;
}

export function detachJobListeners(): void {
  for (const unlisten of unlistenJobs) unlisten();
  unlistenJobs = [];
  stopPoll();
}

export function resetSingleView(): void {
  if (single.running) return;
  single.result = null;
  single.error = null;
  single.steps = [];
  single.pct = 0;
  single.message = "";
  single.jobId = null;
}

export function batchItemLabel(path: string): string {
  const parts = path.split(/[\\/]+/);
  return parts[parts.length - 1] ?? path;
}

export function openJobOutputFolder(): void {
  const path = single.result?.output_path;
  if (!path) return;
  const idx = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  const folder = idx > 0 ? path.slice(0, idx) : path;
  void engineApi.openPath(folder, "folder").catch(() => toast("err", t("protect.openFailed")));
}

export function goToVerifyFromResult(): void {
  const path = single.result?.output_path;
  if (!path) return;
  ui.verifyPrefill = path;
  ui.page = "verify";
}
