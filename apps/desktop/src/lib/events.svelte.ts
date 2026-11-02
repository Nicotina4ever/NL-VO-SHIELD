/** Cablaggio unico degli eventi host → store (job, stato motore, log, modelli). */
import { ENGINE_EVENTS, onEvent } from "./ipc";
import {
  applyEngineStatus,
  applyModelProgress,
  finishModelDownload,
  loadModelSettings,
  loadModels,
  pushLog,
} from "./engine.svelte";
import { attachJobListeners, detachJobListeners } from "./run.svelte";
import type { EngineLogLine, EngineStatusEvent, ModelProgressEvent } from "./types";

let unlisteners: (() => void)[] = [];

async function refreshModels(): Promise<void> {
  await loadModels().catch(() => undefined);
  await loadModelSettings().catch(() => undefined);
}

export async function attachAllListeners(): Promise<void> {
  if (unlisteners.length > 0) return;
  await attachJobListeners();
  const attached = await Promise.all([
    onEvent<EngineStatusEvent>(ENGINE_EVENTS.status, (event) => {
      if (event && typeof event.state === "string") applyEngineStatus(event.state, event.message ?? null);
    }),
    onEvent<EngineLogLine>(ENGINE_EVENTS.log, (event) => {
      if (event && typeof event.line === "string") pushLog(event);
    }),
    onEvent<ModelProgressEvent>(ENGINE_EVENTS.modelsProgress, (event) => {
      if (event && typeof event.model_id === "string") applyModelProgress(event);
    }),
    onEvent<{ model_id?: string; job_id?: string }>(ENGINE_EVENTS.modelsFinished, (event) => {
      finishModelDownload(event?.model_id, null);
      void refreshModels();
    }),
    onEvent<{ model_id?: string; job_id?: string; result?: { error?: string } }>(
      ENGINE_EVENTS.modelsFailed,
      (event) => {
        finishModelDownload(event?.model_id, event?.result?.error ?? "download failed");
        void refreshModels();
      },
    ),
    onEvent<{ reason?: string }>(ENGINE_EVENTS.modelsChanged, () => {
      void refreshModels();
    }),
  ]);
  unlisteners = attached;
}

export function detachAllListeners(): void {
  for (const unlisten of unlisteners) unlisten();
  unlisteners = [];
  detachJobListeners();
}
