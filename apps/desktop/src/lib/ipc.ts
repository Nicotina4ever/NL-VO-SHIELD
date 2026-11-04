/**
 * Layer IPC tipizzato verso l'host Rust (comandi Tauri).
 *
 * Ogni comando accetta un singolo oggetto `request` (serde camelCase lato Rust)
 * così i nomi dei parametri non dipendono dalla conversione automatica Tauri.
 * Gli errori arrivano sempre strutturati: { code, message, detail }.
 */
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { t } from "./i18n.svelte";
import type {
  CacheClearResult,
  ChainStep,
  DoctorResult,
  EngineInfo,
  EngineLogLine,
  Health,
  HostInfo,
  InstallHint,
  JobStatusResult,
  ModelDeleteResult,
  ModelDownloadResult,
  ModelSettings,
  ModelsPayload,
  NlErrorPayload,
  Preset,
  Profile,
  Receipt,
  SettingsPayload,
  UpdateInfo,
  VerifyResult,
  WaveformData,
} from "./types";

export class NlError extends Error {
  code: string;
  detail?: unknown;

  constructor(payload: NlErrorPayload) {
    super(payload.message || payload.code);
    this.name = "NlError";
    this.code = payload.code;
    this.detail = payload.detail;
  }

  /** Testo compatto per la UI: messaggio + codice. */
  get label(): string {
    return `${this.message} (${this.code})`;
  }
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function normalizeError(err: unknown): NlError {
  if (err instanceof NlError) return err;
  if (typeof err === "string") return new NlError({ code: "HOST_ERROR", message: err });
  if (err && typeof err === "object") {
    const o = err as Record<string, unknown>;
    if (typeof o.code === "string" && typeof o.message === "string") {
      return new NlError({ code: o.code, message: o.message, detail: o.detail });
    }
    if (typeof o.message === "string") {
      return new NlError({ code: "HOST_ERROR", message: o.message, detail: err });
    }
  }
  return new NlError({ code: "HOST_ERROR", message: t("errors.HOST_ERROR"), detail: err });
}

/** Normalizza qualsiasi errore host in NlError (per store e UI). */
export function toNlError(err: unknown): NlError {
  return normalizeError(err);
}

async function call<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri()) {
    throw new NlError({
      code: "NOT_TAURI",
      message: t("common.notTauriHint"),
    });
  }
  try {
    return await invoke<T>(cmd, args ?? {});
  } catch (err) {
    throw normalizeError(err);
  }
}

/* ------------------------------- eventi ---------------------------------- */

export const ENGINE_EVENTS = {
  progress: "engine://progress",
  finished: "engine://finished",
  failed: "engine://failed",
  status: "engine://status",
  log: "engine://log",
  modelsProgress: "models://progress",
  modelsFinished: "models://finished",
  modelsFailed: "models://failed",
  modelsChanged: "models://changed",
  labProgress: "lab://progress",
  labFinished: "lab://finished",
  labFailed: "lab://failed",
} as const;

export async function onEvent<T>(name: string, handler: (payload: T) => void): Promise<UnlistenFn> {
  if (!isTauri()) return () => {};
  try {
    return await listen<T>(name, (event) => handler(event.payload));
  } catch {
    return () => {};
  }
}

/* ------------------------------- comandi --------------------------------- */

export const engineApi = {
  health: () => call<Health>("engine_health"),
  restart: () => call<{ ok: boolean; message?: string }>("engine_restart"),
  logs: (limit = 250) => call<{ lines: EngineLogLine[] }>("engine_logs", { request: { limit } }),
  openEngineLog: () =>
    call<{ opened: boolean; path: string; lines: number }>("open_engine_log"),
  listEngines: () => call<{ engines: EngineInfo[] }>("list_engines"),
  installHint: (engineId: string) =>
    call<InstallHint>("engine_install_hint", { request: { engineId } }),
  startJob: (payload: {
    inputPath: string;
    outputDir: string;
    chain: ChainStep[];
    options?: Record<string, unknown>;
    preset?: string;
  }) => call<{ job_id: string }>("start_job", { request: payload }),
  cancelJob: (jobId: string) =>
    call<{ cancelled: boolean }>("cancel_job", { request: { jobId } }),
  jobStatus: (jobId: string) => call<JobStatusResult>("job_status", { request: { jobId } }),
  jobList: () => call<{ jobs: unknown[] }>("job_list"),
  startBatch: (payload: {
    inputs: string[];
    outputDir: string;
    chain: ChainStep[];
    options?: Record<string, unknown>;
    preset?: string;
  }) => call<{ job_id: string }>("start_batch", { request: payload }),
  verifyFile: (path: string, receiptsDir?: string) =>
    call<VerifyResult>("verify_file", { request: { path, receiptsDir: receiptsDir ?? null } }),
  analyzeQuality: (path: string, referencePath?: string) =>
    call<Record<string, unknown>>("analyze_quality", {
      request: { path, referencePath: referencePath ?? null },
    }),
  runDoctor: () => call<DoctorResult>("run_doctor"),
  listProfiles: () => call<{ profiles: Profile[] }>("list_profiles"),
  saveProfile: (profile: Profile) => call<{ profile: Profile }>("save_profile", { request: { profile } }),
  deleteProfile: (id: string) => call<{ deleted: boolean }>("delete_profile", { request: { id } }),
  listPresets: () => call<{ presets: Preset[] }>("list_presets"),
  savePreset: (preset: Preset) => call<{ preset: Preset }>("save_preset", { request: { preset } }),
  listReceipts: () => call<{ receipts: Receipt[] }>("list_receipts"),
  verifyReceipt: (id: string) =>
    call<{ valid: boolean; reason?: string; receipt?: Receipt }>("verify_receipt", { request: { id } }),
  getSettings: () => call<SettingsPayload>("get_settings"),
  setSettings: (settings: Record<string, unknown>, host?: Record<string, unknown>) =>
    call<SettingsPayload>("set_settings", { request: { settings, host: host ?? null } }),
  openPath: (path: string, kind: "file" | "folder" = "file") =>
    call<{ opened: boolean }>("open_path", { request: { path, kind } }),
  exportReport: (content: string, fileName?: string, path?: string) =>
    call<{ saved: boolean; path?: string }>("export_report", {
      request: { content, fileName: fileName ?? null, path: path ?? null },
    }),
  readWaveform: (path: string, buckets = 1600) =>
    call<WaveformData>("read_waveform", { request: { path, buckets } }),
  readFileBase64: (path: string, maxBytes = 96 * 1024 * 1024) =>
    call<{ base64: string; size: number }>("read_file_base64", { request: { path, maxBytes } }),
  hostInfo: () => call<HostInfo>("host_info"),
};

export const modelsApi = {
  list: () => call<ModelsPayload>("list_models"),
  download: (modelId: string) =>
    call<ModelDownloadResult>("download_model", { request: { modelId } }),
  cancelDownload: (modelId: string, jobId?: string | null) =>
    call<{ cancelled: boolean }>("cancel_model_download", {
      request: { modelId, jobId: jobId ?? null },
    }),
  remove: (modelIds: string[]) =>
    call<ModelDeleteResult>("delete_model", { request: { modelIds } }),
  clearCache: (scope: "cache" | "tmp" | "all" = "cache") =>
    call<CacheClearResult>("clear_cache", { request: { scope } }),
  getSettings: () => call<ModelSettings>("get_model_settings"),
  setSettings: (patch: {
    modelsDir?: string | null;
    cacheDir?: string | null;
    portable?: boolean | null;
  }) => call<ModelSettings>("set_model_settings", { request: patch }),
};

export const updatesApi = {
  check: (force = false) => call<UpdateInfo>("check_updates", { request: { force } }),
};

export interface AiVariant {
  id: "cpu" | "gpu";
  label: string;
  available: boolean;
  description: string;
  expected_download_bytes: number;
  device: string;
}

export interface AiRuntimeStatus {
  id: string;
  name: string;
  version: string;
  installed: boolean;
  state: "not_installed" | "available" | "error" | string;
  path: string;
  python: string | null;
  bootstrap_python: string | null;
  variant: string | null;
  device: string | null;
  size_bytes: number;
  installed_at: string | null;
  error: string | null;
  expected_download_bytes: number | null;
  variants: AiVariant[];
  gpu: { available: boolean; name: string | null };
  models_ready: boolean;
}

export interface AiJobStatus {
  job_id: string;
  state?: "queued" | "running" | "completed" | "failed" | "cancelled" | string;
  status?: "queued" | "running" | "completed" | "failed" | "cancelled" | string;
  progress?: { pct?: number | null; message?: string; downloaded?: number; total?: number | null } | null;
  result?: unknown;
  error?: { code?: string; message?: string } | null;
}

export const aiApi = {
  status: () => call<{ runtime: AiRuntimeStatus }>("ai_status"),
  install: (variant: "cpu" | "gpu") =>
    call<{ job_id: string; variant: string }>("ai_install", { request: { variant } }),
  uninstall: (removeModels = false) =>
    call<{ ok: boolean; freed_bytes: number; removed_models: string[]; runtime_removed: boolean }>(
      "ai_uninstall",
      { request: { removeModels } },
    ),
  verify: () => call<{ ok: boolean; message?: string; confidence?: number }>("ai_verify"),
  jobStatus: (jobId: string) => call<AiJobStatus>("job_status", { request: { jobId } }),
  cancelJob: (jobId: string) => call<{ cancelled: boolean }>("cancel_job", { request: { jobId } }),
};

/* ------------------------------ laboratorio ------------------------------ */

export const labApi = {
  status: () => call<unknown>("lab_status"),
  installModel: (request: { id: string; source?: string; localPath?: string | null }) =>
    call<unknown>("lab_install_model", { request }),
  uninstallModel: (id: string) => call<unknown>("lab_uninstall_model", { request: { id } }),
  installWhisper: (request: {
    model: string;
    source?: string;
    localPath?: string | null;
  }) => call<unknown>("lab_install_whisper", { request }),
  uninstallWhisper: (model: string) =>
    call<unknown>("lab_uninstall_whisper", { request: { model } }),
  transcribe: (request: { audio: string; model: string; language?: string; device?: string }) =>
    call<unknown>("lab_transcribe", { request }),
  verify: (deep = false) => call<unknown>("lab_verify", { deep }),
  run: (request: {
    original: string;
    protected: string;
    modelId: string;
    text: string;
    refText?: string;
    language?: string;
    seed?: number | null;
    device?: string;
    outputDir?: string | null;
  }) => call<unknown>("lab_run", { request }),
};


export const componentsJobsApi = {
  jobs: () => call<{ jobs: any[] }>("components_jobs"),
  job: (jobId: string) => call<{ job: any }>("components_job", { jobId }),
};

export const componentsApi = {
  list: () => call<{ items: ComponentInfo[]; total_bytes: number }>("components_list"),
  action: (request: { id: string; action: string; localPath?: string | null; confirmed?: boolean }) =>
    call<{ component: Record<string, unknown> }>("components_action", { request }),
};

export interface ComponentInfo {
  id: string;
  label: string;
  kind: string;
  threats: string[];
  builtin: boolean;
  removable: boolean;
  installed: boolean;
  enabled: boolean;
  path: string | null;
  size_bytes: number;
  requirements: string;
  description: string;
  actions: string[];
  bootstrap?: boolean;
  runtime_present?: boolean;
  manual_missing?: string[];
}


export interface MicromambaEnv { name: string; path: string; size_bytes: number }
export interface MicromambaStatus {
  version: string; installed: boolean; binary: string; root: string;
  envs: MicromambaEnv[]; size_bytes: number; platform: string; asset: string;
}
export const micromambaApi = {
  status: () => call<{ micromamba: MicromambaStatus }>("micromamba_status"),
  install: () => call<unknown>("micromamba_install"),
  verify: () => call<unknown>("micromamba_verify"),
  uninstall: () => call<unknown>("micromamba_uninstall"),
  createEnv: (name: string, python = "3.10") =>
    call<unknown>("micromamba_create_env", { request: { name, python } }),
  removeEnv: (name: string) =>
    call<unknown>("micromamba_remove_env", { request: { name, python: "3.10" } }),
};
