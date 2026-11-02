/**
 * Preset integrati e costruzione della catena di protezione.
 *
 * I preset qui sono composizioni esplicite di passi del contratto
 * (docs/ARCHITECTURE.md §3.4). Non implicano che il motore sia disponibile:
 * la UI mostra lo stato reale di ogni engine prima di avviare.
 */
import type { ChainStep } from "./types";

export type Strength = "light" | "medium" | "high";

export interface ChainConfig {
  antiClone: boolean;
  watermark: boolean;
  c2pa: boolean;
  strength: Strength;
  /** Motore anti-clonazione scelto (default spectral_cloak). */
  antiCloneEngine?: string;
  /** Motore watermark scelto: audioseal se disponibile, altrimenti watermark_dsp. */
  watermarkEngine?: string;
  payloadHex?: string;
  manifestTitle?: string;
  author?: string;
  usageTerms?: string;
}

export interface BuiltinPreset {
  id: string;
  nameKey: string;
  descKey: string;
  antiClone: boolean;
  watermark: boolean;
  c2pa: boolean;
  strength: Strength;
  custom?: boolean;
}

export const ENGINE_IDS = {
  antiClone: "spectral_cloak",
  watermark: "audioseal",
  c2pa: "c2pa",
} as const;

export const BUILTIN_PRESETS: BuiltinPreset[] = [
  {
    id: "studio_master",
    nameKey: "protect.presetStudio",
    descKey: "protect.presetStudioDesc",
    antiClone: true,
    watermark: true,
    c2pa: true,
    strength: "high",
  },
  {
    id: "professional_demo",
    nameKey: "protect.presetDemo",
    descKey: "protect.presetDemoDesc",
    antiClone: true,
    watermark: true,
    c2pa: true,
    strength: "light",
  },
  {
    id: "public_distribution",
    nameKey: "protect.presetPublic",
    descKey: "protect.presetPublicDesc",
    antiClone: true,
    watermark: true,
    c2pa: true,
    strength: "medium",
  },
  {
    id: "custom",
    nameKey: "protect.presetCustom",
    descKey: "protect.presetCustomDesc",
    antiClone: true,
    watermark: true,
    c2pa: true,
    strength: "medium",
    custom: true,
  },
];

export function findPreset(id: string): BuiltinPreset | undefined {
  return BUILTIN_PRESETS.find((preset) => preset.id === id);
}

/** Catena nell'ordine consigliato dal contratto: anti-clone → watermark → C2PA. */
export function buildChain(config: ChainConfig): ChainStep[] {
  const chain: ChainStep[] = [];

  if (config.antiClone) {
    chain.push({
      engine: config.antiCloneEngine?.trim() || ENGINE_IDS.antiClone,
      action: "perturb",
      required: true,
      params: { strength: config.strength },
    });
  }

  if (config.watermark) {
    const params: Record<string, unknown> = { sample_rate: 16000 };
    const payload = (config.payloadHex ?? "").trim();
    if (payload) params.payload_hex = payload;
    chain.push({
      engine: config.watermarkEngine?.trim() || ENGINE_IDS.watermark,
      action: "embed",
      required: true,
      params,
    });
  }

  if (config.c2pa) {
    const manifest: Record<string, unknown> = {
      title: config.manifestTitle?.trim() || undefined,
      creator: config.author?.trim() || undefined,
      usage_terms: config.usageTerms?.trim() || undefined,
      ai_generated: false,
      tool: "NL VO Shield",
    };
    for (const key of Object.keys(manifest)) {
      if (manifest[key] === undefined) delete manifest[key];
    }
    chain.push({
      engine: ENGINE_IDS.c2pa,
      action: "sign",
      required: true,
      params: { manifest, cert: "auto" },
    });
  }

  return chain;
}

/** Riepilogo leggibile della catena (per UI e report). */
export function describeChain(chain: ChainStep[]): string[] {
  return chain.map((step) => `${step.engine}.${step.action}${step.required ? "" : " (opzionale)"}`);
}

export const STRENGTHS: Strength[] = ["light", "medium", "high"];
