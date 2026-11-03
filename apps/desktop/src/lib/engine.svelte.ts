/** Stato del motore/host: connessione, registro engine, profili, preset, modelli, log. */
import {
  engineApi,
  modelsApi,
  updatesApi,
  NlError,
  toNlError,
} from "./ipc";
import { t } from "./i18n.svelte";
import type {
  EngineInfo,
  EngineLogLine,
  Health,
  HostInfo,
  ModelInfo,
  ModelProgressEvent,
  ModelSettings,
  ModelsPayload,
  Preset,
  Profile,
  Receipt,
  SettingsPayload,
  UpdateInfo,
} from "./types";

export type ConnState = "unknown" | "starting" | "ready" | "error";

export const engine = $state({
  conn: "unknown" as ConnState,
  online: false,
  booting: false,
  health: null as Health | null,
  error: null as NlError | null,
  host: null as HostInfo | null,
  hostError: null as NlError | null,
  engines: [] as EngineInfo[],
  enginesLoading: false,
  enginesLoaded: false,
  settings: null as SettingsPayload | null,
  profiles: [] as Profile[],
  presets: [] as Preset[],
  receipts: [] as Receipt[],
  models: [] as ModelInfo[],
  modelsMeta: null as ModelsPayload | null,
  modelsLoading: false,
  modelSettings: null as ModelSettings | null,
  /** modelId → job_id del download in corso (per l'annullamento). */
  modelJobs: {} as Record<string, string>,
  update: null as UpdateInfo | null,
  logs: [] as EngineLogLine[],
  restarting: false,
});

const MAX_LOGS = 600;

export function isEngineReady(): boolean {
  return engine.online && engine.conn === "ready";
}

/** Connessione iniziale: host info (sempre locale) + handshake con il motore. */
export async function bootEngine(): Promise<void> {
  engine.booting = true;
  engine.conn = "starting";
  await Promise.allSettled([loadHost(), connect()]);
  engine.booting = false;
}

export async function connect(): Promise<boolean> {
  engine.conn = engine.online ? "ready" : "starting";
  try {
    const health = await engineApi.health();
    engine.health = health;
    engine.online = true;
    engine.conn = "ready";
    engine.error = null;
    void refreshEngines().catch(() => undefined);
    return true;
  } catch (err) {
    engine.online = false;
    engine.conn = "error";
    engine.error = toNlError(err);
    return false;
  }
}

export async function loadHost(): Promise<void> {
  try {
    engine.host = await engineApi.hostInfo();
    engine.hostError = null;
  } catch (err) {
    engine.hostError = toNlError(err);
  }
}

export async function refreshEngines(): Promise<EngineInfo[]> {
  engine.enginesLoading = true;
  try {
    const result = await engineApi.listEngines();
    engine.engines = Array.isArray(result.engines) ? result.engines : [];
    engine.enginesLoaded = true;
    return engine.engines;
  } catch (err) {
    engine.enginesLoaded = true;
    engine.error = toNlError(err);
    return engine.engines;
  } finally {
    engine.enginesLoading = false;
  }
}

export function engineById(id: string): EngineInfo | undefined {
  return engine.engines.find((item) => item.id === id);
}

/** Un engine è selezionabile solo se non è dichiarato indisponibile. */
export function isEngineUsable(id: string): boolean {
  const info = engineById(id);
  if (!info) return false;
  return info.status !== "unavailable";
}

export function engineUsableReason(id: string): string | null {
  const info = engineById(id);
  if (!info) return t("engines.notInRegistry");
  if (info.status === "unavailable") {
    return info.reason
      ? t("engines.unavailableWithReason", { reason: info.reason })
      : t("engines.unavailablePlain");
  }
  return null;
}

export async function restartEngine(): Promise<boolean> {
  engine.restarting = true;
  try {
    await engineApi.restart();
    engine.logs = [];
    const ok = await connect();
    return ok;
  } catch (err) {
    engine.error = toNlError(err);
    engine.online = false;
    engine.conn = "error";
    return false;
  } finally {
    engine.restarting = false;
  }
}

export async function loadSettings(): Promise<void> {
  try {
    engine.settings = await engineApi.getSettings();
  } catch (err) {
    engine.error = toNlError(err);
  }
}

export async function saveSettings(
  settings: Record<string, unknown>,
  host: Record<string, unknown>,
): Promise<SettingsPayload> {
  const payload = await engineApi.setSettings(settings, host);
  engine.settings = payload;
  await loadHost();
  return payload;
}

export async function loadProfiles(): Promise<void> {
  try {
    const result = await engineApi.listProfiles();
    engine.profiles = Array.isArray(result.profiles) ? result.profiles : [];
  } catch (err) {
    engine.error = toNlError(err);
    engine.profiles = [];
  }
}

export async function loadPresets(): Promise<void> {
  try {
    const result = await engineApi.listPresets();
    engine.presets = Array.isArray(result.presets) ? result.presets : [];
  } catch {
    engine.presets = [];
  }
}

export async function loadReceipts(): Promise<void> {
  try {
    const result = await engineApi.listReceipts();
    engine.receipts = Array.isArray(result.receipts) ? result.receipts : [];
  } catch (err) {
    engine.error = toNlError(err);
    engine.receipts = [];
  }
}

export async function loadModels(): Promise<void> {
  engine.modelsLoading = true;
  try {
    const payload = await modelsApi.list();
    engine.modelsMeta = payload;
    engine.models = Array.isArray(payload.models) ? payload.models : [];
  } catch (err) {
    engine.error = toNlError(err);
  } finally {
    engine.modelsLoading = false;
  }
}

export async function loadModelSettings(): Promise<void> {
  try {
    engine.modelSettings = await modelsApi.getSettings();
  } catch (err) {
    engine.error = toNlError(err);
  }
}

export function modelsForEngine(engineId: string): ModelInfo[] {
  return engine.models.filter((model) => model.engineId === engineId);
}

export function engineWeightsState(engineId: string): "present" | "missing" | "manual" | "unknown" {
  const models = modelsForEngine(engineId);
  if (models.length === 0) return "manual";
  if (models.some((model) => model.present)) return "present";
  return "missing";
}

export async function checkUpdates(force = false): Promise<UpdateInfo | null> {
  try {
    const info = await updatesApi.check(force);
    engine.update = info;
    return info;
  } catch (err) {
    engine.update = {
      configured: false,
      repo: "Nicotina4ever/NL-VO-SHIELD",
      currentVersion: engine.host?.appVersion ?? "0.0.0",
      autoUpdateAvailable: false,
      error: toNlError(err).message,
      reason: null,
      upToDate: null,
    };
    return engine.update;
  }
}

export async function refreshLogs(limit = 250): Promise<void> {
  try {
    const result = await engineApi.logs(limit);
    engine.logs = Array.isArray(result.lines) ? result.lines : [];
  } catch {
    /* log non disponibili: nessun errore bloccante */
  }
}

/* ------------------------- aggiornamenti da eventi ------------------------ */

export function applyEngineStatus(state: string, message?: string | null): void {
  if (state === "ready") {
    engine.conn = "ready";
    engine.online = true;
  } else if (state === "crashed" || state === "stopped") {
    engine.conn = "error";
    engine.online = false;
    engine.error = new NlError({
      code: "ENGINE_EXITED",
      message: message ?? t("errors.ENGINE_EXITED"),
    });
  } else if (state === "starting" || state === "restarting") {
    engine.conn = "starting";
    engine.online = false;
  }
}

export function pushLog(line: EngineLogLine): void {
  engine.logs = [...engine.logs.slice(-(MAX_LOGS - 1)), line];
}

export function applyModelProgress(event: ModelProgressEvent): void {
  const modelId = event?.model_id;
  if (!modelId) return;
  engine.models = engine.models.map((model) =>
    model.id === modelId
      ? {
          ...model,
          downloading: true,
          progress: event.pct ?? model.progress ?? 0,
          error: null,
        }
      : model,
  );
}

/** Fine download (evento finished/failed): reset stato e aggiorna l'elenco. */
export function finishModelDownload(modelId: string | undefined, error?: string | null): void {
  const id = modelId ?? "";
  if (id) {
    engine.models = engine.models.map((model) =>
      model.id === id
        ? { ...model, downloading: false, progress: null, error: error ?? null }
        : model,
    );
    const nextJobs = { ...engine.modelJobs };
    delete nextJobs[id];
    engine.modelJobs = nextJobs;
  }
}

export function setModelDownloading(modelId: string, downloading: boolean, jobId?: string | null): void {
  engine.models = engine.models.map((model) =>
    model.id === modelId ? { ...model, downloading, progress: downloading ? 0 : null } : model,
  );
  const nextJobs = { ...engine.modelJobs };
  if (downloading && jobId) nextJobs[modelId] = jobId;
  else delete nextJobs[modelId];
  engine.modelJobs = nextJobs;
}

export type { EngineLogLine, Health, HostInfo };
