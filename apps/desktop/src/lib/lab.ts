/**
 * Laboratorio di prova (Qwen3-TTS + Whisper) — tipi, normalizzazione e catalogo.
 *
 * Contratto motore reale (engine-core, nlvs.qwen-lab-status/1):
 *   lab.status → { lab: { runtime, qwen:{models[]}, whisper:{models[]},
 *                         device{cpu,cuda,cuda_devices,mps}, paths, ... } }
 *   lab.run    → job con result { outputs:{original_clone, protected_clone},
 *                                 quality{...}, speaker_similarity{...} }
 *
 * La UI resta tollerante a varianti camelCase/snake_case e a backend assente.
 */

export type LabModelKind = "qwen" | "whisper";

export interface LabDevice {
  id: string;
  label: string;
  supported: boolean;
  reason?: string | null;
  detail?: string | null;
}

export interface LabModelInfo {
  id: string;
  label: string;
  kind: LabModelKind;
  params?: string | null;
  sizeBytes?: number | null;
  installed: boolean;
  path?: string | null;
  downloadable: boolean;
  reason?: string | null;
  estimated: boolean;
  statusText?: string | null;
}

export interface LabStatusInfo {
  available: boolean;
  error?: string | null;
  models: LabModelInfo[];
  whisper: LabModelInfo[];
  devices: LabDevice[];
  runtimeBytes?: number | null;
  modelsBytes?: number | null;
  freeBytes?: number | null;
  modelsDir?: string | null;
  dataDir?: string | null;
  runtimeInstalled?: boolean;
  raw?: unknown;
}

export interface LabRunRequest {
  original: string;
  protected: string;
  modelId: string;
  text: string;
  refText: string;
  language: string;
  seed: number | null;
  device: string;
  outputDir?: string | null;
}

export interface LabOutput {
  /** chiave motore: original_clone / protected_clone, oppure path libero */
  key?: string | null;
  path: string | null;
  label?: string | null;
  metrics?: Record<string, unknown> | null;
  speakerSimilarity?: number | null;
  raw?: unknown;
}

export interface LabRunOutcome {
  outputs: LabOutput[];
  metrics?: Record<string, unknown> | null;
  /** delta similarity (protetto − originale) quando fornito dal motore. */
  speakerSimilarity?: number | null;
  raw: unknown;
}

export interface LabPair {
  id: string;
  original: string;
  protected: string | null;
  originalIsProtected: boolean;
}

/** Catalogo di ripiego: solo quando `lab.status` non è disponibile. */
const QWEN_FALLBACK: LabModelInfo[] = [
  {
    id: "qwen3-tts-0.6b",
    label: "Qwen3-TTS 0.6B Base",
    kind: "qwen",
    params: "0.6B",
    sizeBytes: 2_516_000_000,
    installed: false,
    downloadable: true,
    estimated: true,
    reason: "backend",
  },
  {
    id: "qwen3-tts-1.7b",
    label: "Qwen3-TTS 1.7B Base",
    kind: "qwen",
    params: "1.7B",
    sizeBytes: 4_544_000_000,
    installed: false,
    downloadable: true,
    estimated: true,
    reason: "backend",
  },
];

const WHISPER_FALLBACK: LabModelInfo[] = [
  { id: "tiny", label: "Whisper tiny", kind: "whisper", params: "tiny", sizeBytes: 75_000_000, installed: false, downloadable: true, estimated: true, reason: "backend" },
  { id: "base", label: "Whisper base", kind: "whisper", params: "base", sizeBytes: 145_000_000, installed: false, downloadable: true, estimated: true, reason: "backend" },
  { id: "small", label: "Whisper small", kind: "whisper", params: "small", sizeBytes: 484_000_000, installed: false, downloadable: true, estimated: true, reason: "backend" },
  { id: "medium", label: "Whisper medium", kind: "whisper", params: "medium", sizeBytes: 1_530_000_000, installed: false, downloadable: true, estimated: true, reason: "backend" },
  { id: "large-v3", label: "Whisper large-v3", kind: "whisper", params: "large-v3", sizeBytes: 3_100_000_000, installed: false, downloadable: true, estimated: true, reason: "backend" },
];

function readNumber(value: unknown): number | null {  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function readBool(value: unknown): boolean {
  return value === true || value === 1 || value === "true";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function modelFromEngine(
  entry: unknown,
  kind: LabModelKind,
): LabModelInfo | null {
  const item = asRecord(entry);
  if (!item) return null;
  if (kind === "qwen" && readString(item.kind)?.includes("tokenizer")) return null;
  const id =
    readString(item.id) ??
    (readString(item.dirname) ? `whisper-${readString(item.dirname)}` : null) ??
    null;
  if (!id) return null;
  const installed =
    readBool(item.installed) ||
    readBool(item.present) ||
    (Array.isArray(item.missing) && item.missing.length === 0 && readBool(item.installed));
  const approx = readNumber(item.approx_bytes) ?? readNumber(item.approxBytes);
  const onDisk = readNumber(item.size_bytes) ?? readNumber(item.sizeBytes) ?? 0;
  const sizeBytes = onDisk && onDisk > 0 ? onDisk : approx;
  const missing = Array.isArray(item.missing) ? item.missing.length : 0;
  const dirname = readString(item.dirname);
  const shortId =
    kind === "whisper" && dirname ? dirname : id.replace(/^whisper-/, "");
  return {
    id: kind === "whisper" ? shortId : id,
    label: readString(item.label) ?? id,
    kind,
    params: kind === "whisper" ? shortId : readString(item.params),
    sizeBytes,
    installed,
    path:
      readString(item.path) ??
      readString(item.target_path) ??
      readString(item.targetPath) ??
      null,
    downloadable: true,
    reason: missing > 0 ? `missing:${missing}` : (readString(item.reason) ?? null),
    estimated: !onDisk || onDisk <= 0,
    statusText: readString(item.status),
  };
}

/** Normalizza `lab.status` (contratto reale `{lab:{...}}`, varianti tollerate). */
export function normalizeLabStatus(rawInput: unknown): LabStatusInfo {
  const wrapper = asRecord(rawInput) ?? {};
  const root = asRecord(wrapper.lab) ?? wrapper;
  const qwenSection = asRecord(root.qwen) ?? {};
  const whisperSection = asRecord(root.whisper) ?? {};
  const paths = asRecord(root.paths) ?? {};
  const runtime = asRecord(root.runtime) ?? {};
  const deviceInfo = asRecord(root.device) ?? {};

  const qwenRaw = Array.isArray(qwenSection.models)
    ? qwenSection.models
    : collectModelArray(root, ["models", "qwen_models", "qwenModels"]);
  const whisperRaw = Array.isArray(whisperSection.models)
    ? whisperSection.models
    : collectModelArray(root, ["whisper_models", "whisperModels"]);

  const models = qwenRaw
    .map((entry) => modelFromEngine(entry, "qwen"))
    .filter(
      (entry): entry is LabModelInfo =>
        entry !== null && (!entry.statusText || entry.statusText.includes("model")),
    );
  const whisper = whisperRaw
    .map((entry) => modelFromEngine(entry, "whisper"))
    .filter((entry): entry is LabModelInfo => entry !== null);

  const devices: LabDevice[] = [];
  if (Object.keys(deviceInfo).length > 0) {
    const cudaDevices = Array.isArray(deviceInfo.cuda_devices)
      ? deviceInfo.cuda_devices.filter((value): value is string => typeof value === "string")
      : [];
    devices.push({
      id: "cpu",
      label: "CPU",
      supported: deviceInfo.cpu !== false,
      reason: null,
      detail: null,
    });
    devices.push({
      id: "cuda",
      label: "GPU (CUDA)",
      supported: readBool(deviceInfo.cuda),
      reason: readBool(deviceInfo.cuda)
        ? null
        : readString(deviceInfo.note) ?? "CUDA non rilevata",
      detail: cudaDevices.join(", ") || null,
    });
    if (Array.isArray(deviceInfo.requested_options) && deviceInfo.requested_options.includes("auto")) {
      devices.push({ id: "auto", label: "Auto", supported: true, reason: null, detail: null });
    }
  } else {
    devices.push(
      { id: "cpu", label: "CPU", supported: true, reason: null, detail: null },
      { id: "cuda", label: "GPU (CUDA)", supported: false, reason: "CUDA non rilevata", detail: null },
    );
  }

  const hasData = Boolean(root.qwen || root.whisper || root.runtime || root.paths);
  return {
    available: hasData,
    error: hasData ? null : "lab.status senza dati",
    models: models.length > 0 ? models : [],
    whisper: whisper.length > 0 ? whisper : [],
    devices,
    runtimeBytes: readNumber(runtime.size_bytes) ?? readNumber(runtime.sizeBytes) ?? null,
    modelsBytes: null,
    freeBytes: readNumber(root.free_bytes) ?? readNumber(root.freeBytes) ?? null,
    modelsDir:
      readString(paths.models_dir) ??
      readString(paths.modelsDir) ??
      readString(qwenSection.models_root) ??
      null,
    dataDir: readString(paths.data_dir) ?? readString(paths.dataDir) ?? null,
    runtimeInstalled: readBool(runtime.installed),
    raw: rawInput,
  };
}

function collectModelArray(root: Record<string, unknown>, keys: string[]): unknown[] {
  for (const key of keys) {
    const value = root[key];
    if (Array.isArray(value)) return value;
  }
  return [];
}

/** Stato di ripiego quando `lab.status` non è disponibile. */
export function offlineLabStatus(error: string): LabStatusInfo {
  return {
    available: false,
    error,
    models: QWEN_FALLBACK.map((entry) => ({ ...entry })),
    whisper: WHISPER_FALLBACK.map((entry) => ({ ...entry })),
    devices: [
      { id: "cpu", label: "CPU", supported: true, reason: null, detail: null },
      { id: "cuda", label: "GPU (CUDA)", supported: false, reason: "backend non pronto", detail: null },
    ],
    runtimeBytes: null,
    modelsBytes: null,
    freeBytes: null,
    modelsDir: null,
    dataDir: null,
    raw: null,
  };
}

export function pairWavs(files: string[]): LabPair[] {
  const normalized = files.filter((file) => file.toLowerCase().endsWith(".wav"));
  const originals: string[] = [];
  const protectedFiles: string[] = [];
  for (const file of normalized) {
    if (file.toLowerCase().endsWith("_protected.wav")) protectedFiles.push(file);
    else originals.push(file);
  }
  const pairs: LabPair[] = [];
  const stemOf = (file: string): string => file.replace(/\.wav$/i, "").toLowerCase();
  for (const original of originals) {
    const stem = stemOf(original);
    const protectedMatch = protectedFiles.find(
      (candidate) => stemOf(candidate.replace(/_protected$/i, "")) === stem,
    );
    pairs.push({
      id: `${original}|${protectedMatch ?? ""}`,
      original,
      protected: protectedMatch ?? null,
      originalIsProtected: false,
    });
  }
  for (const protectedFile of protectedFiles) {
    const stem = stemOf(protectedFile.replace(/_protected$/i, ""));
    if (!originals.some((candidate) => stemOf(candidate) === stem)) {
      pairs.push({
        id: `${protectedFile}|`,
        original: protectedFile,
        protected: null,
        originalIsProtected: true,
      });
    }
  }
  return pairs;
}

function unwrapJobResult(input: unknown): Record<string, unknown> {
  let current = asRecord(input) ?? {};
  for (let depth = 0; depth < 4; depth += 1) {
    const nested = asRecord(current.result);
    if (!nested) break;
    if (current.outputs || current.quality || current.speaker_similarity) break;
    current = nested;
  }
  return current;
}

function cloneLabels(): Record<string, string> {
  return {
    original_clone: "lab.results.cloneOriginal",
    protected_clone: "lab.results.cloneProtected",
  };
}

/** Estrae output, metriche e speaker similarity dal risultato reale di `lab.run`. */
export function normalizeLabOutcome(rawResult: unknown): LabRunOutcome {
  const result = unwrapJobResult(rawResult);
  const outputs: LabOutput[] = [];
  const quality = asRecord(result.quality) ?? {};
  const similarity = asRecord(result.speaker_similarity) ?? {};
  const labels = cloneLabels();
  const outputsField = asRecord(result.outputs);
  if (outputsField) {
    for (const [key, value] of Object.entries(outputsField)) {
      const item = asRecord(value);
      if (!item) continue;
      const path =
        readString(item.path) ?? readString(item.output) ?? readString(item.file) ?? null;
      if (!path) continue;
      const qualityEntry = asRecord(quality[key]);
      const similarityEntry = asRecord(similarity[`${key}_vs_reference`]);
      const rawSimilarity =
        readNumber(similarityEntry?.similarity) ??
        readNumber(item.similarity) ??
        (typeof item.similarity === "number" ? item.similarity : null);
      outputs.push({
        key,
        path,
        label: labels[key] ? key : key,
        metrics: asRecord(qualityEntry?.metrics) ?? asRecord(item.metrics) ?? null,
        speakerSimilarity:
          rawSimilarity != null ? (rawSimilarity <= 1 ? rawSimilarity : rawSimilarity / 100) : null,
        raw: value,
      });
    }
  } else {
    for (const key of ["outputs", "files", "results", "artifacts"]) {
      const value = result[key];
      if (!Array.isArray(value)) continue;
      for (const entry of value) {
        if (typeof entry === "string") {
          outputs.push({ path: entry, label: null, metrics: null, raw: entry });
          continue;
        }
        const item = asRecord(entry);
        if (!item) continue;
        const path =
          readString(item.path) ??
          readString(item.output) ??
          readString(item.file) ??
          readString(item.cloned) ??
          null;
        if (!path) continue;
        outputs.push({
          path,
          label: readString(item.label) ?? readString(item.key),
          metrics: asRecord(item.metrics) ?? null,
          speakerSimilarity: readNumber(item.speaker_similarity) ?? readNumber(item.similarity),
          raw: entry,
        });
      }
    }
  }

  const delta =
    readNumber(similarity.delta_protected_minus_original) ??
    readNumber(similarity.deltaProtectedMinusOriginal) ??
    null;
  return {
    outputs,
    metrics: null,
    speakerSimilarity: delta,
    raw: rawResult,
  };
}

/** Etichette leggibili per le metriche note, senza inventare valori. */
export function metricLabel(key: string): string {
  const map: Record<string, string> = {
    lufs: "LUFS",
    lufs_integrated: "LUFS",
    peak_dbfs: "Sample peak (dBFS)",
    true_peak_dbfs: "True peak (dBFS)",
    clipping_samples: "Clipping",
    snr_db: "SNR (dB)",
    sample_rate: "Sample rate (Hz)",
    duration_seconds: "Durata (s)",
    speaker_similarity: "Speaker similarity",
    similarity: "Speaker similarity",
  };
  return map[key] ?? key.replace(/_/g, " ");
}
