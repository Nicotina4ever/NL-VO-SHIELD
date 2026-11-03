/**
 * Preset a 3 livelli (+ Personalizzato) e costruzione della catena di protezione.
 *
 * La catena è sempre ESPLICITA: il preset propone quali motori usare, l'utente
 * può attivarli/disattivarli singolarmente e l'ordine inviato al motore è:
 * anti-clonazione → watermark → provenance (C2PA sempre ultimo, dopo ogni
 * trasformazione audio).
 *
 * Nessun motore viene dichiarato "attivo" da qui: lo stato reale arriva da
 * `engines.list` del core Python.
 */
import type { ChainStep } from "./types";

export type Strength = "light" | "medium" | "high";
export type StepRole = "anti_clone" | "watermark" | "provenance";

export interface EngineRef {
  id: string;
  name?: string;
  kind?: string;
  status?: string;
}

export interface Selection {
  engine: string;
  role: StepRole;
}

export const ROLE_ORDER: StepRole[] = ["anti_clone", "watermark", "provenance"];

export function roleForKind(kind: string | undefined | null): StepRole | null {
  if (kind === "anti_clone") return "anti_clone";
  if (kind === "watermark") return "watermark";
  if (kind === "provenance") return "provenance";
  return null;
}

export function actionForRole(role: StepRole): string {
  if (role === "anti_clone") return "perturb";
  if (role === "watermark") return "embed";
  return "sign";
}

/** Parametri di default per motore (l'engine valida e applica i suoi). */
export function paramsForEngine(
  _engine: string,
  role: StepRole,
  options: {
    strength: Strength;
    payloadHex?: string;
    manifestTitle?: string;
    author?: string;
    usageTerms?: string;
    engineParams?: Record<string, Record<string, unknown>>;
  },
): Record<string, unknown> {
  const extra = options.engineParams?.[_engine] ?? {};
  if (role === "anti_clone") {
    // Il motore accetta low/medium/high (non "light").
    return { strength: ENGINE_STRENGTH[options.strength], ...extra };
  }
  if (role === "watermark") {
    const params: Record<string, unknown> = { sample_rate: 16000 };
    const payload = (options.payloadHex ?? "").trim();
    if (payload) params.payload_hex = payload;
    return { ...params, ...extra };
  }
  const manifest: Record<string, unknown> = {
    title: options.manifestTitle?.trim() || undefined,
    creator: options.author?.trim() || undefined,
    usage_terms: options.usageTerms?.trim() || undefined,
    ai_generated: false,
    tool: "NL VO Shield",
  };
  for (const key of Object.keys(manifest)) {
    if (manifest[key] === undefined) delete manifest[key];
  }
  return { manifest, cert: "auto", ...extra };
}

/** Catena nell'ordine consigliato dal contratto, solo con i motori selezionati. */
export function buildChain(
  selections: Selection[],
  options: {
    strength: Strength;
    payloadHex?: string;
    manifestTitle?: string;
    author?: string;
    usageTerms?: string;
    engineParams?: Record<string, Record<string, unknown>>;
  },
): ChainStep[] {
  // Raggruppa per ruolo (anti-clone → watermark → provenance) mantenendo
  // l'ordine scelto dall'utente all'interno dello stesso ruolo.
  const ordered: Selection[] = [];
  for (const role of ROLE_ORDER) {
    for (const selection of selections) {
      if (selection.role === role) ordered.push(selection);
    }
  }
  return ordered.map((selection) => ({
    engine: selection.engine,
    action: actionForRole(selection.role),
    required: true,
    params: paramsForEngine(selection.engine, selection.role, options),
  }));
}

export interface ChainWarning {
  key: string;
  level: "warn" | "error";
}

/** Avvisi onesti su combinazioni che rischiano di degradare l'audio. */
export function chainWarnings(selections: Selection[], strength: Strength): ChainWarning[] {
  const warnings: ChainWarning[] = [];
  const antiClone = selections.filter((selection) => selection.role === "anti_clone").length;
  const watermark = selections.filter((selection) => selection.role === "watermark").length;
  if (selections.length === 0) {
    warnings.push({ key: "protect.warnEmptyChain", level: "error" });
    return warnings;
  }
  if (antiClone > 1) {
    warnings.push({ key: "protect.warnMultipleAnticlone", level: "warn" });
  }
  if (antiClone >= 1 && strength === "high" && selections.length >= 3) {
    warnings.push({ key: "protect.warnHighStrength", level: "warn" });
  }
  if (watermark > 1) {
    warnings.push({ key: "protect.warnMultipleWatermark", level: "warn" });
  }
  if (!selections.some((selection) => selection.role === "provenance")) {
    warnings.push({ key: "protect.warnNoC2pa", level: "warn" });
  }
  return warnings;
}

export interface BuiltinPreset {
  id: string;
  level: 1 | 2 | 3 | 0;
  nameKey: string;
  descKey: string;
  impactKey: string;
  engines: string[];
  strength: Strength;
  custom?: boolean;
}

/**
 * Tre livelli di protezione + Personalizzato.
 * Gli id motore sono quelli del contratto; la UI li sostituisce con il primo
 * motore DISPONIBILE della stessa categoria se quello indicato non lo è.
 */
export const BUILTIN_PRESETS: BuiltinPreset[] = [
  {
    id: "light_publication",
    level: 1,
    nameKey: "protect.presetLight",
    descKey: "protect.presetLightDesc",
    impactKey: "protect.presetLightImpact",
    engines: ["audioseal", "c2pa"],
    strength: "light",
  },
  {
    id: "balanced_demo",
    level: 2,
    nameKey: "protect.presetBalanced",
    descKey: "protect.presetBalancedDesc",
    impactKey: "protect.presetBalancedImpact",
    engines: ["spectral_cloak", "audioseal", "c2pa"],
    strength: "light",
  },
  {
    id: "studio_master",
    level: 3,
    nameKey: "protect.presetStudio",
    descKey: "protect.presetStudioDesc",
    impactKey: "protect.presetStudioImpact",
    engines: ["spectral_cloak", "audioseal", "c2pa"],
    strength: "high",
  },
  {
    id: "custom",
    level: 0,
    nameKey: "protect.presetCustom",
    descKey: "protect.presetCustomDesc",
    impactKey: "protect.presetCustomImpact",
    engines: [],
    strength: "medium",
    custom: true,
  },
];

export function findPreset(id: string): BuiltinPreset | undefined {
  return BUILTIN_PRESETS.find((preset) => preset.id === id);
}

/**
 * Converte gli id motore del preset in selezioni reali, sostituendo un motore
 * non disponibile con il primo disponibile della stessa categoria.
 */
export function resolvePresetSelections(preset: BuiltinPreset, engines: EngineRef[]): Selection[] {
  const usable = (id: string): boolean => {
    const found = engines.find((engine) => engine.id === id);
    return !found || found.status !== "unavailable";
  };
  const firstUsableForRole = (role: StepRole): EngineRef | undefined =>
    engines.find((engine) => roleForKind(engine.kind) === role && engine.status !== "unavailable") ??
    engines.find((engine) => roleForKind(engine.kind) === role);

  const selections: Selection[] = [];
  for (const engineId of preset.engines) {
    const found = engines.find((engine) => engine.id === engineId);
    const role = roleForKind(found?.kind);
    if (!role) continue;
    if (usable(engineId)) {
      selections.push({ engine: engineId, role });
      continue;
    }
    const fallback = firstUsableForRole(role);
    if (fallback && !selections.some((selection) => selection.engine === fallback.id)) {
      selections.push({ engine: fallback.id, role });
    }
  }
  return selections;
}

export function describeChain(chain: ChainStep[]): string[] {
  return chain.map((step) => `${step.engine}.${step.action}${step.required ? "" : " (opzionale)"}`);
}

export const STRENGTHS: Strength[] = ["light", "medium", "high"];

/** Valori accettati dal motore anti-clonazione (spectral_cloak). */
export const ENGINE_STRENGTH: Record<Strength, string> = {
  light: "low",
  medium: "medium",
  high: "high",
};
