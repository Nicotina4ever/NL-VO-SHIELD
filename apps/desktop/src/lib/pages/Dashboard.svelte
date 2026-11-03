<script lang="ts">
  import { onMount } from "svelte";
  import { engineStateLabel, t, hostErrorMessage } from "../i18n.svelte";
  import {
    bootEngine,
    connect,
    engine,
    loadHost,
    refreshEngines,
    engineWeightsState,
  } from "../engine.svelte";
  import { history } from "../run.svelte";
  import { navigate, ui } from "../ui.svelte";
  import { basename, formatDate, jobStatusTone } from "../format";
  import { engineApi } from "../ipc";
  import { toast } from "../ui.svelte";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";

  const available = $derived(engine.engines.filter((item) => item.status !== "unavailable").length);
  const unavailable = $derived(engine.engines.filter((item) => item.status === "unavailable").length);
  const pythonSourceKey = $derived.by(() => {
    const source = engine.host?.python?.source;
    if (!source) return "diagnostics.sourceMissing";
    const map: Record<string, string> = {
      env: "diagnostics.sourceEnv",
      bundled: "diagnostics.sourceBundled",
      dev_project: "diagnostics.sourceDevProject",
      local_runtime: "diagnostics.sourceLocalRuntime",
      frozen: "diagnostics.sourceFrozen",
      missing: "diagnostics.sourceMissing",
    };
    return map[source] ?? "diagnostics.sourceMissing";
  });

  async function openDataDir(): Promise<void> {
    const path = engine.host?.paths.dataDir;
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
      if (engine.conn !== "ready") await connect();
      if (!engine.enginesLoaded) await refreshEngines();
    })();
  });
</script>

<div class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("dashboard.title")}</h1>
      <p>{t("dashboard.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn btn--sm" onclick={() => void bootEngine()} disabled={engine.booting}>
        {#if engine.booting}<span class="spinner"></span>{/if}
        {t("dashboard.refreshEngine")}
      </button>
    </div>
  </div>

  {#if !engine.online && !engine.booting}
    <div class="warn-box">
      <div class="warn-box__title">
        <Icon name="alert" size={15} />
        {t("dashboard.setupWarning")}
      </div>
      <div>{hostErrorMessage(engine.error) || t("dashboard.setupWarningHint")}</div>
      <div class="small" style="margin-top:6px">
        {t("diagnostics.pythonSource")}: <strong>{t(pythonSourceKey)}</strong>
        {#if engine.host?.python?.path}
          · <span class="mono break-all">{engine.host.python.path}</span>
        {/if}
      </div>
      <div class="btn-row" style="margin-top:8px">
        <button class="btn btn--sm btn--primary" onclick={() => navigate("diagnostics")}>
          {t("dashboard.openDiagnostics")}
        </button>
        <button class="btn btn--sm" onclick={() => void connect()}>{t("common.retry")}</button>
      </div>
    </div>
  {/if}

  <div class="grid grid-3">
    <div class="card">
      <div class="card__head">
        <h2>{t("dashboard.engineOnline")}</h2>
        <span class="spacer"></span>
        <span class="dot {engine.conn === 'ready' ? 'dot--ok' : engine.booting ? 'dot--warn' : 'dot--err'}"></span>
      </div>
      <div class="card__body">
        <dl class="kv">
          <dt>{t("dashboard.engineVersion")}</dt>
          <dd class="mono">{engine.health?.version ?? "—"}</dd>
          <dt>{t("dashboard.appVersion")}</dt>
          <dd class="mono">{engine.host?.appVersion ?? "—"}</dd>
          <dt>{t("dashboard.platform")}</dt>
          <dd class="mono">
            {engine.host?.os ?? "—"} / {engine.host?.arch ?? "—"}
          </dd>
          <dt>{t("dashboard.enginePython")}</dt>
          <dd class="mono small break-all">{engine.host?.python?.path ?? t("common.notAvailable")}</dd>
        </dl>
      </div>
    </div>

    <div class="card">
      <div class="card__head">
        <h2>{t("dashboard.enginesSummary")}</h2>
        <span class="spacer"></span>
        <button class="btn btn--ghost btn--sm" onclick={() => navigate("engines")}>
          {t("common.details")}
        </button>
      </div>
      <div class="card__body">
        <div class="row" style="gap:18px;align-items:baseline">
          <div>
            <div style="font-size:26px;font-weight:650">{available}</div>
            <div class="small muted">{t("dashboard.enginesReady")}</div>
          </div>
          <div>
            <div style="font-size:26px;font-weight:650">{unavailable}</div>
            <div class="small muted">{t("dashboard.enginesUnavailable")}</div>
          </div>
        </div>
        <div class="col" style="gap:4px;margin-top:10px">
          {#each engine.engines.slice(0, 5) as item (item.id)}
            <div class="row gap-sm small">
              <span
                class="dot {item.status === 'unavailable'
                  ? 'dot--err'
                  : item.status === 'experimental'
                    ? 'dot--warn'
                    : 'dot--ok'}"
              ></span>
              <span class="mono">{item.id}</span>
              <span class="spacer"></span>
              <span class="faint">
                {engineWeightsState(item.id) === "present"
                  ? t("engines.weightsPresent")
                  : engineWeightsState(item.id) === "missing"
                    ? t("engines.weightsMissing")
                    : ""}
              </span>
            </div>
          {:else}
            <span class="small faint">{t("common.loading")}</span>
          {/each}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card__head">
        <h2>{t("dashboard.hostInfo")}</h2>
      </div>
      <div class="card__body">
        <dl class="kv">
          <dt>{t("dashboard.dataDir")}</dt>
          <dd class="mono small break-all">{engine.host?.paths.dataDir ?? "—"}</dd>
          <dt>{t("dashboard.dataLocation")}</dt>
          <dd>
            {engine.host?.paths.portable ? t("settings.dataDirInstall") : t("settings.dataDirUser")}
          </dd>
        </dl>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn btn--sm" onclick={() => void openDataDir()} disabled={!engine.host}>
            <Icon name="folder" size={14} />
            {t("dashboard.openDataDir")}
          </button>
          <button class="btn btn--sm btn--ghost" onclick={() => navigate("settings")}>
            {t("nav.settings")}
          </button>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("dashboard.quickStart")}</h2>
      <span class="spacer"></span>
      <span class="small faint">{t("app.tagline")}</span>
    </div>
    <div class="card__body">
      <div class="grid grid-3">
        <button class="dropzone dropzone--sm" style="cursor:pointer" onclick={() => navigate("protect")}>
          <Icon name="shield" size={24} />
          <span class="dropzone__title">{t("dashboard.newProtection")}</span>
          <span class="small">{t("dashboard.newProtectionHint")}</span>
        </button>
        <button class="dropzone dropzone--sm" style="cursor:pointer" onclick={() => navigate("verify")}>
          <Icon name="verify" size={24} />
          <span class="dropzone__title">{t("dashboard.verifyFile")}</span>
          <span class="small">{t("dashboard.verifyFileHint")}</span>
        </button>
        <button class="dropzone dropzone--sm" style="cursor:pointer" onclick={() => navigate("diagnostics")}>
          <Icon name="diagnostics" size={24} />
          <span class="dropzone__title">{t("dashboard.runDiagnostics")}</span>
          <span class="small">{t("dashboard.runDiagnosticsHint")}</span>
        </button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("dashboard.recentJobs")}</h2>
      <span class="spacer"></span>
      <span class="small faint">{history.items.length}</span>
    </div>
    <div class="card__body {history.items.length === 0 ? '' : 'card__body--flush'}">
      {#if history.items.length === 0}
        <div class="empty">{t("dashboard.noJobs")}</div>
      {:else}
        <table class="table">
          <thead>
            <tr>
              <th>{t("common.file")}</th>
              <th>{t("common.status")}</th>
              <th>{t("common.date", {})}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each history.items as item (item.job_id)}
              <tr>
                <td class="truncate" style="max-width:340px" title={item.output_path ?? ""}>
                  {basename(item.output_path) !== "—" ? basename(item.output_path) : item.job_id}
                </td>
                <td>
                  <Badge
                    label={engineStateLabel(item.status)}
                    tone={jobStatusTone(item.status)}
                  />
                </td>
                <td class="small faint mono">{formatDate(item.finished_at ?? item.started_at)}</td>
                <td>
                  {#if item.output_path}
                    <button
                      class="btn btn--ghost btn--sm"
                      onclick={() => {
                        ui.verifyPrefill = item.output_path ?? null;
                        navigate("verify");
                      }}
                    >
                      {t("dashboard.verifyFile")}
                    </button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  </div>
</div>
