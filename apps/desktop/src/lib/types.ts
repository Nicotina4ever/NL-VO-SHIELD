/**
 * Tipi del contratto host Rust <-> engine Python e della UI.
 * Riferimento: docs/ARCHITECTURE.md §3 (protocollo), §3.3 EngineInfo, §3.5 JobResult,
 * §3.6 VerifyResult, §4 (comandi Tauri).
 *
 * Nota: i payload ricchi arrivano dal motore come JSON e possono contenere campi
 * in più. Le interfacce usano index signature dove il contratto non è chiuso.
 */

export type EngineStatus = "available" | "unavailable" | "experimental" | "verified";

export interface EngineInfo {
  id: string;
  name: string;
  version: string;
  kind: string;
  capabilities: string[];
  supported_os: string[];
  status: EngineStatus;
  requires: string[];
  requires_download: boolean;
  reason?: string | null;
  path?: string | null;
  quality_note?: string | null;
  [key: string]: unknown;
}

export interface Health {
  version: string;
  os: string;
  arch: string;
  python: string;
  engines?: number | unknown;
  data_dir: string;
  [key: string]: unknown;
}

export interface ChainStep {
  engine: string;
  action: string;
  required: boolean;
  params: Record<string, unknown>;
}

export interface JobExecuted {
  engine: string;
  action: string;
  version?: string | null;
  params?: Record<string, unknown> | null;
}

export interface JobVerified {
  check: string;
  ok: boolean;
  detail?: Record<string, unknown> | null;
}

export interface JobSkipped {
  engine: string;
  reason?: string | null;
}

export interface JobFailed {
  engine: string;
  error?: string | null;
  required?: boolean;
}

export type JobStatus =
  | "protected"
  | "partial"
  | "failed"
  | "indeterminate"
  | "cancelled"
  | "running"
  | "queued"
  | "unknown";

export interface QualityMetrics {
  sample_rate?: number | null;
  channels?: number | null;
  duration_sec?: number | null;
  peak_dbfs?: number | null;
  true_peak_dbfs?: number | null;
  lufs?: number | null;
  lufs_integrated?: number | null;
  clipping_samples?: number | null;
  snr_db?: number | null;
  spectral_shift?: number | null;
  [key: string]: unknown;
}

export interface JobResult {
  job_id: string;
  status: JobStatus;
  requested?: string[] | null;
  executed?: JobExecuted[] | null;
  verified?: JobVerified[] | null;
  skipped?: JobSkipped[] | null;
  failed?: JobFailed[] | null;
  quality?: { input?: QualityMetrics | null; output?: QualityMetrics | null; [key: string]: unknown } | null;
  hashes?: { input_sha256?: string | null; output_sha256?: string | null; [key: string]: unknown } | null;
  output_path?: string | null;
  output_partial_path?: string | null;
  receipt_id?: string | null;
  warnings?: string[] | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

export interface ProgressEvent {
  job_id: string;
  step?: string | null;
  step_index?: number | null;
  step_total?: number | null;
  pct?: number | null;
  message?: string | null;
  [key: string]: unknown;
}

export interface JobStatusResult {
  job_id: string;
  state: string;
  progress?: number | Record<string, unknown> | null;
  result?: JobResult | null;
  error?: string | null;
  [key: string]: unknown;
}

export interface VerifyWatermark {
  detected?: boolean | null;
  confidence?: number | null;
  payload_hex?: string | null;
  engine?: string | null;
  [key: string]: unknown;
}

export interface VerifyC2pa {
  present?: boolean | null;
  valid?: boolean | null;
  signer?: string | null;
  trusted?: boolean | null;
  claims?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface VerifyFingerprint {
  matched?: boolean | null;
  receipt_id?: string | null;
  similarity?: number | null;
  [key: string]: unknown;
}

export interface VerifyReceipt {
  found?: boolean | null;
  id?: string | null;
  valid?: boolean | null;
  [key: string]: unknown;
}

export type VerifyOutcome = "verified" | "partial" | "not_detected" | "indeterminate" | string;

export interface VerifyResult {
  path: string;
  sha256?: string | null;
  watermark?: VerifyWatermark | null;
  c2pa?: VerifyC2pa | null;
  fingerprint?: VerifyFingerprint | null;
  receipt?: VerifyReceipt | null;
  outcome: VerifyOutcome;
  details?: Record<string, unknown> | null;
  warnings?: string[] | null;
  [key: string]: unknown;
}

export interface DoctorCheck {
  id: string;
  status: string;
  label?: string | null;
  detail?: unknown;
  [key: string]: unknown;
}

export interface DoctorResult {
  checks: DoctorCheck[];
  summary?: string | Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface Profile {
  id: string;
  name: string;
  org?: string | null;
  contact?: string | null;
  usage_terms?: string | null;
  notes?: string | null;
  default_preset?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
}

export interface Preset {
  id: string;
  name: string;
  description?: string | null;
  chain?: ChainStep[] | null;
  builtin?: boolean | null;
  [key: string]: unknown;
}

export interface Receipt {
  id: string;
  job_id?: string | null;
  created_at?: string | null;
  valid?: boolean | null;
  [key: string]: unknown;
}

export interface InstallHint {
  instructions: string;
  requires_download: boolean;
  [key: string]: unknown;
}

/* ------------------------------- host side ------------------------------- */

export type PythonSource =
  | "env"
  | "bundled"
  | "dev_project"
  | "local_runtime"
  | "frozen"
  | "missing";

export interface PythonResolution {
  found: boolean;
  path?: string | null;
  source: PythonSource;
  module: string;
  /** `script` (launcher nlvs_rpc.py), `frozen` (binario) o `module` (fallback -m). */
  mode: string;
  launcher?: string | null;
  tried: string[];
  error?: string | null;
}

export interface HostPaths {
  dataDir: string;
  modelsDir: string;
  cacheDir: string;
  outputDir: string;
  configDir: string;
  settingsFile: string;
  installDir: string;
  installWritable: boolean;
  /** true quando manca una cartella dati scrivibile: la UI deve chiederla. */
  needsDataDir: boolean;
  suggestedDataDir: string;
  portable: boolean;
  portableAvailable: boolean;
  portableReason?: string | null;
  /** `install` (dentro l'installazione), `user` (scelta dall'utente), `unresolved`. */
  dataDirSource: string;
}

export interface HardwareInfo {
  /** true quando CPU e RAM sono stati letti davvero dal sistema. */
  detected: boolean;
  cpuModel?: string | null;
  cpuLogicalCores?: number | null;
  ramTotalBytes?: number | null;
  gpuName?: string | null;
  gpuVramBytes?: number | null;
  gpuVendor?: string | null;
  /** `nvidia-smi`, `system` oppure assente. */
  gpuSource?: string | null;
  /** Note oneste su ciò che non è stato possibile rilevare. */
  notes: string[];
}

export interface HostInfo {
  appVersion: string;
  productName: string;
  identifier: string;
  os: string;
  arch: string;
  webview?: string | null;
  paths: HostPaths;
  python: PythonResolution;
  engineModule: string;
  repoRoot?: string | null;
  language: string;
  theme: string;
  updater: UpdaterStatus;
  /** CPU/RAM/GPU/VRAM rilevati dall'host (sola lettura, nessuna rete). */
  hardware?: HardwareInfo | null;
}

export interface UpdaterStatus {
  autoUpdateEnabled: boolean;
  releaseCheckAvailable: boolean;
  repo: string;
  reason?: string | null;
}

export interface UpdateInfo {
  configured: boolean;
  repo: string;
  currentVersion: string;
  latestVersion?: string | null;
  releaseUrl?: string | null;
  releaseName?: string | null;
  notes?: string | null;
  publishedAt?: string | null;
  upToDate?: boolean | null;
  checkedAt?: string | null;
  reason?: string | null;
  error?: string | null;
  autoUpdateAvailable: boolean;
}

export interface SettingsPayload {
  settings: Record<string, unknown>;
  host: HostSettings;
  engineError?: NlErrorPayload | null;
}

export interface HostSettings {
  language: string;
  theme: string;
  outputDir?: string | null;
  openAfterJob: boolean;
  portable: boolean;
  dataDir?: string | null;
  dataDirOverride?: string | null;
  modelsDir?: string | null;
  cacheDir?: string | null;
  autoUpdateCheck: boolean;
  [key: string]: unknown;
}

export interface ModelInfo {
  id: string;
  label: string;
  engineId: string;
  kind: string;
  present: boolean;
  path?: string | null;
  sizeBytes: number;
  expectedSizeBytes?: number | null;
  sha256?: string | null;
  expectedSha256?: string | null;
  integrity?: string | null;
  source?: string | null;
  licenseNote?: string | null;
  reason?: string | null;
  details?: string | null;
  downloadAllowed: boolean;
  requiresDownload: boolean;
  requiredFor?: string[] | null;
  externalFound?: string[] | null;
  externalMissing?: string[] | null;
  /** stato UI: download in corso + progresso locale */
  downloading?: boolean;
  progress?: number | null;
  error?: string | null;
}

export interface ModelsPayload {
  models: ModelInfo[];
  dirs: { modelsDir: string; cacheDir: string };
  totalBytes: number;
}

export interface ModelProgressEvent {
  job_id?: string;
  model_id: string;
  pct?: number;
  message?: string;
  downloaded?: number;
  total?: number;
}

export interface ModelDownloadResult {
  modelId: string;
  jobId: string;
  started: boolean;
}

export interface ModelDeleteResult {
  deleted: string[];
  failures: { modelId: string; error: unknown }[];
  freedBytes: number;
}

export interface CacheClearResult {
  freedBytes: number;
  cleared: Record<string, unknown>;
}

export interface ModelSettings {
  dataDir: string;
  modelsDir: string;
  cacheDir: string;
  portable: boolean;
  portableAvailable: boolean;
  portableReason?: string | null;
  downloadedBytes: number;
  cacheBytes: number;
  writable: boolean;
  engineError?: NlErrorPayload | null;
}

export interface WaveformData {
  supported: boolean;
  reason?: string | null;
  sampleRate: number;
  channels: number;
  bitsPerSample: number;
  durationSec: number;
  frames: number;
  peaks: { min: number[]; max: number[] };
  /** Inviluppo RMS per bucket, allineato a peaks. */
  rms?: number[];
  source: string;
}

export interface EngineLogLine {
  ts: string;
  level: string;
  line: string;
}

/** Traccia audio per il player A/B (percorso assoluto restituito dal motore). */
export interface AudioTrack {
  label: string;
  path: string;
}

export interface EngineStatusEvent {
  state: "starting" | "ready" | "crashed" | "stopped" | "restarting";
  message?: string | null;
  detail?: unknown;
}

/* ------------------------------- errors ---------------------------------- */

export interface NlErrorPayload {
  code: string;
  message: string;
  detail?: unknown;
}
