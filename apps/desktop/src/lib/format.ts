/** Formattazione e piccoli helper condivisi dalla UI. */

export function formatBytes(bytes: number | null | undefined, decimals = 1): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(decimals)} ${units[i]}`;
}

export function formatPct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const clamped = Math.max(0, Math.min(100, value));
  return `${clamped.toFixed(clamped < 10 ? 1 : 0)}%`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  try {
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(d);
  } catch {
    return d.toISOString();
  }
}

export function formatNumber(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

export function shortHash(hash: string | null | undefined, length = 16): string {
  if (!hash) return "—";
  if (hash.length <= length) return hash;
  return `${hash.slice(0, length)}…`;
}

export function basename(path: string | null | undefined): string {
  if (!path) return "—";
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

export function dirname(path: string | null | undefined): string {
  if (!path) return "";
  const idx = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  return idx > 0 ? path.slice(0, idx) : path;
}

export function fileExtension(path: string | null | undefined): string {
  if (!path) return "";
  const base = basename(path);
  const idx = base.lastIndexOf(".");
  return idx >= 0 ? base.slice(idx + 1).toLowerCase() : "";
}

/** Etichetta/tono del badge per un esito job. */
export function jobStatusTone(status: string | null | undefined): "ok" | "warn" | "err" | "info" | "muted" {
  switch (status) {
    case "protected":
      return "ok";
    case "partial":
      return "warn";
    case "failed":
    case "cancelled":
      return "err";
    case "indeterminate":
      return "info";
    case "running":
    case "queued":
      return "info";
    default:
      return "muted";
  }
}

export function verifyOutcomeTone(outcome: string | null | undefined): "ok" | "warn" | "err" | "info" | "muted" {
  switch (outcome) {
    case "verified":
      return "ok";
    case "partial":
      return "warn";
    case "not_detected":
      return "err";
    case "indeterminate":
      return "info";
    default:
      return "muted";
  }
}

export function engineStatusTone(status: string | null | undefined): "ok" | "warn" | "err" | "info" | "muted" {
  switch (status) {
    case "verified":
      return "ok";
    case "available":
      return "info";
    case "experimental":
      return "warn";
    case "unavailable":
      return "err";
    default:
      return "muted";
  }
}

export function checkStatusTone(status: string | null | undefined): "ok" | "warn" | "err" | "info" | "muted" {
  const s = (status ?? "").toLowerCase();
  if (s === "ok" || s === "pass" || s === "passed") return "ok";
  if (s === "warn" || s === "warning" || s === "degraded") return "warn";
  if (s === "fail" || s === "failed" || s === "error") return "err";
  if (s === "skip" || s === "skipped") return "info";
  return "muted";
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function uid(prefix = "id"): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

export function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  return Promise.reject(new Error("clipboard unavailable"));
}
