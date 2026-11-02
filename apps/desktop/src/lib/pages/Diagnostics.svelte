<script lang="ts">
  import { onMount } from "svelte";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import { toast, navigate } from "../ui.svelte";
  import {
    engine,
    loadHost,
    refreshEngines,
    refreshLogs,
    restartEngine,
  } from "../engine.svelte";
  import { engineApi } from "../ipc";
  import { pickSavePath } from "../dialogs";
  import { checkStatusTone, copyToClipboard } from "../format";
  import type { DoctorResult } from "../types";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";
  import Modal from "../components/Modal.svelte";

  let doctor = $state<DoctorResult | null>(null);
  let doctorError = $state<string | null>(null);
  let running = $state(false);
  let pendingRestart = $state(false);
  let logsLoading = $state(false);

  const checks = $derived(doctor?.checks ?? []);
  const failed = $derived(checks.filter((check) => checkStatusTone(check.status) === "err").length);
  const warned = $derived(checks.filter((check) => checkStatusTone(check.status) === "warn").length);
  const okCount = $derived(checks.filter((check) => checkStatusTone(check.status) === "ok").length);

  const pythonSourceKey = $derived.by(() => {
    const source = engine.host?.python?.source;
    const map: Record<string, string> = {
      env: "diagnostics.sourceEnv",
      bundled: "diagnostics.sourceBundled",
      dev_project: "diagnostics.sourceDevProject",
      local_runtime: "diagnostics.sourceLocalRuntime",
      frozen: "diagnostics.sourceFrozen",
      missing: "diagnostics.sourceMissing",
    };
    return map[source ?? "missing"] ?? "diagnostics.sourceMissing";
  });

  const logText = $derived(
    engine.logs.map((line) => `${line.ts ?? ""} ${line.line ?? ""}`.trim()).join("\n"),
  );

  function detailText(detail: unknown): string {
    if (detail == null) return "";
    if (typeof detail === "string") return detail;
    try {
      return JSON.stringify(detail);
    } catch {
      return String(detail);
    }
  }

  async function runDoctor(): Promise<void> {
    running = true;
    doctorError = null;
    try {
      doctor = await engineApi.runDoctor();
    } catch (err) {
      doctorError = hostErrorMessage(err as { code?: string; message?: string });
      doctor = null;
    } finally {
      running = false;
    }
  }

  async function doRestart(): Promise<void> {
    pendingRestart = false;
    const ok = await restartEngine();
    if (ok) toast("ok", t("diagnostics.engineRestartDone"));
    else toast("err", t("common.error"), t("errors.ENGINE_START_FAILED"));
    await loadHost();
    await refreshEngines();
  }

  async function refreshLogsNow(): Promise<void> {
    logsLoading = true;
    try {
      await refreshLogs(400);
    } finally {
      logsLoading = false;
    }
  }

  function reportPayload(): Record<string, unknown> {
    return {
      generated_at: new Date().toISOString(),
      app: engine.host?.appVersion ?? null,
      engine: engine.health ?? null,
      host: engine.host ?? null,
      engines: engine.engines,
      doctor,
      logs: engine.logs.slice(-200),
    };
  }

  async function copyReport(): Promise<void> {
    try {
      await copyToClipboard(JSON.stringify(reportPayload(), null, 2));
      toast("ok", t("diagnostics.reportCopied"));
    } catch {
      toast("err", t("common.error"));
    }
  }

  async function exportReport(): Promise<void> {
    const target = await pickSavePath({
      title: t("diagnostics.exportReport"),
      defaultPath: `nl-vo-shield-diagnostics-${Date.now()}.json`,
      filters: [{ name: "JSON", extensions: ["json"] }],
    });
    if (!target) return;
    try {
      const response = await engineApi.exportReport(
        JSON.stringify(reportPayload(), null, 2),
        "diagnostics.json",
        target,
      );
      if (response.saved) toast("ok", t("common.done"), response.path ?? target);
    } catch (err) {
      toast("err", t("common.error"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  async function openConfigDir(): Promise<void> {
    const path = engine.host?.paths.configDir;
    if (!path) return;
    try {
      await engineApi.openPath(path, "folder");
    } catch {
      toast("err", t("protect.openFailed"));
    }
  }

  onMount(() => {
    void (async () => {
      if (!engine.host) await loadHost();
      await refreshLogs(300);
    })();
  });
</script>

<div class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("diagnostics.title")}</h1>
      <p>{t("diagnostics.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn btn--primary btn--sm" onclick={() => void runDoctor()} disabled={running || !engine.online}>
        {#if running}<span class="spinner"></span>{/if}
        <Icon name="diagnostics" size={14} />
        {t("diagnostics.runDoctor")}
      </button>
      <button class="btn btn--sm" onclick={() => (pendingRestart = true)} disabled={engine.restarting}>
        {#if engine.restarting}<span class="spinner"></span>{/if}
        {t("diagnostics.engineRestart")}
      </button>
    </div>
  </div>

  {#if !engine.online}
    <div class="warn-box">
      <div class="warn-box__title">{t("diagnostics.engineDown")}</div>
      <div class="btn-row" style="margin-top:8px">
        <button class="btn btn--sm btn--primary" onclick={() => navigate("settings")}>
          {t("nav.settings")}
        </button>
        <button class="btn btn--sm" onclick={() => void doRestart()}>{t("diagnostics.retryEngine")}</button>
      </div>
    </div>
  {/if}

  <div class="grid grid-2">
    <div class="card">
      <div class="card__head">
        <h2>{t("diagnostics.hostInfo")}</h2>
        <span class="spacer"></span>
        <button class="btn btn--ghost btn--sm" onclick={() => void openConfigDir()}>
          <Icon name="folder" size={14} />
          {t("diagnostics.openConfigDir")}
        </button>
      </div>
      <div class="card__body">
        <dl class="kv">
          <dt>{t("diagnostics.pythonSource")}</dt>
          <dd>{t(pythonSourceKey)}</dd>
          <dt>{t("diagnostics.pythonPath")}</dt>
          <dd class="mono small break-all">{engine.host?.python?.path ?? t("common.notAvailable")}</dd>
          {#if engine.host?.python?.launcher}
            <dt>{t("diagnostics.launcher")}</dt>
            <dd class="mono small break-all">{engine.host.python.launcher}</dd>
          {/if}
          <dt>{t("dashboard.dataDir")}</dt>
          <dd class="mono small break-all">{engine.host?.paths.dataDir ?? "—"}</dd>
          <dt>{t("diagnostics.configDir")}</dt>
          <dd class="mono small break-all">{engine.host?.paths.settingsFile ?? "—"}</dd>
          <dt>{t("diagnostics.modelsDir")}</dt>
          <dd class="mono small break-all">{engine.host?.paths.modelsDir ?? "—"}</dd>
          <dt>{t("diagnostics.cacheDir")}</dt>
          <dd class="mono small break-all">{engine.host?.paths.cacheDir ?? "—"}</dd>
          <dt>{t("dashboard.engineVersion")}</dt>
          <dd class="mono">{engine.health?.version ?? "—"}</dd>
          <dt>{t("dashboard.platform")}</dt>
          <dd class="mono">
            {engine.host?.os ?? "—"} / {engine.host?.arch ?? "—"}
            {#if engine.host?.webview} · {engine.host.webview}{/if}
          </dd>
        </dl>
        {#if engine.host?.python?.tried && engine.host.python.tried.length > 0}
          <details style="margin-top:10px">
            <summary class="small muted" style="cursor:pointer">{t("diagnostics.pythonTried")}</summary>
            <ul class="mono small" style="margin:6px 0 0;padding-left:18px">
              {#each engine.host.python.tried as candidate}
                <li class="break-all">{candidate}</li>
              {/each}
            </ul>
          </details>
        {/if}
      </div>
    </div>

    <div class="card">
      <div class="card__head">
        <h2>{t("diagnostics.doctorTitle")}</h2>
        <span class="spacer"></span>
        {#if doctor}
          <Badge
            label="{checks.length} · {t('dashboard.ready')}"
            tone={failed > 0 ? "err" : warned > 0 ? "warn" : "ok"}
          />
        {/if}
      </div>
      <div class="card__body">
        {#if doctorError}
          <div class="err-box">
            <div class="err-box__title">{t("diagnostics.doctorFailed")}</div>
            <div>{doctorError}</div>
          </div>
        {:else if !doctor}
          <div class="empty">{t("diagnostics.noChecks")}</div>
        {:else}
          <div class="row" style="gap:16px;margin-bottom:10px">
            <div><span class="bold">{okCount}</span> <span class="small muted">OK</span></div>
            <div><span class="bold">{warned}</span> <span class="small muted">WARN</span></div>
            <div><span class="bold">{failed}</span> <span class="small muted">FAIL</span></div>
            <span class="spacer"></span>
            <span class="small faint mono">
              {doctor.summary ? t("diagnostics.summary") : ""}
            </span>
          </div>
          <div class="col" style="gap:6px">
            {#each checks as check (check.id)}
              <div class="list-row">
                <Badge label={check.status} tone={checkStatusTone(check.status)} />
                <div class="list-row__main">
                  <div class="small bold mono">{check.id}</div>
                  {#if check.detail != null}
                    <div class="small faint break-all">{detailText(check.detail)}</div>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
      <div class="card__foot">
        <button class="btn btn--sm" onclick={() => void copyReport()}>
          <Icon name="copy" size={13} />
          {t("diagnostics.copyReport")}
        </button>
        <button class="btn btn--sm btn--ghost" onclick={() => void exportReport()}>
          <Icon name="download" size={13} />
          {t("diagnostics.exportReport")}
        </button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("diagnostics.logs")}</h2>
      <span class="spacer"></span>
      <button class="btn btn--ghost btn--sm" onclick={() => void refreshLogsNow()} disabled={logsLoading}>
        {#if logsLoading}<span class="spinner"></span>{/if}
        {t("diagnostics.refreshLogs")}
      </button>
    </div>
    <div class="card__body">
      {#if logText}
        <pre
          class="mono small"
          style="margin:0;max-height:320px;overflow:auto;white-space:pre-wrap;background:var(--nl-bg-2);border:1px solid var(--nl-border);border-radius:7px;padding:10px">{logText}</pre>
      {:else}
        <div class="empty">{t("diagnostics.noLogs")}</div>
      {/if}
    </div>
  </div>
</div>

{#if pendingRestart}
  <Modal title={t("diagnostics.engineRestart")} onclose={() => (pendingRestart = false)}>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (pendingRestart = false)}>{t("common.cancel")}</button>
      <button class="btn btn--primary" onclick={() => void doRestart()}>{t("common.confirm")}</button>
    {/snippet}
    <p>{t("diagnostics.restartConfirm")}</p>
  </Modal>
{/if}
