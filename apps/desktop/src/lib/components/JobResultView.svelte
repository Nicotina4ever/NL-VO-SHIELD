<script lang="ts">
  import type { JobResult } from "../types";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import {
    basename,
    formatDate,
    formatNumber,
    jobStatusTone,
    shortHash,
  } from "../format";
  import Badge from "./Badge.svelte";
  import { engineApi } from "../ipc";
  import { toast } from "../ui.svelte";

  let {
    result = null,
    onverify = undefined,
    onexport = undefined,
  }: {
    result?: JobResult | null;
    onverify?: (path: string) => void;
    onexport?: (result: JobResult) => void;
  } = $props();

  function statusLabel(status: string): string {
    const key = `status.${status}`;
    const label = t(key);
    return label === key ? t("status.unknown") : label;
  }

  function statusNote(status: string): string {
    if (status === "protected") return t("protect.protectedNote");
    if (status === "partial") return t("protect.partialNote");
    if (status === "failed") return t("protect.failedNote");
    if (status === "cancelled") return t("protect.jobCancelled");
    return t("protect.indeterminateNote");
  }

  async function openFolder(path: string | null | undefined): Promise<void> {
    if (!path) return;
    try {
      const idx = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
      const folder = idx > 0 ? path.slice(0, idx) : path;
      await engineApi.openPath(folder, "folder");
    } catch (err) {
      toast("err", t("protect.openFailed"), hostErrorMessage(err as never));
    }
  }

  function qualityRows(label: string, metrics: Record<string, unknown> | null | undefined): [string, string][] {
    if (!metrics) return [];
    const rows: [string, string][] = [];
    const push = (name: string, value: unknown, suffix = "") => {
      if (value == null || typeof value === "object") return;
      rows.push([name, `${String(value)}${suffix}`]);
    };
    push(t("verify.peak"), metrics.peak_dbfs, " dBFS");
    push(t("verify.truePeak"), metrics.true_peak_dbfs, " dBFS");
    push(t("verify.lufs"), metrics.lufs ?? metrics.lufs_integrated, " LUFS");
    push(t("verify.clipping"), metrics.clipping_samples);
    push(t("verify.snr"), metrics.snr_db, " dB");
    push(t("verify.sampleRate"), metrics.sample_rate, " Hz");
    push(t("verify.channels"), metrics.channels);
    if (rows.length === 0) {
      rows.push([label, t("common.notAvailable")]);
    }
    return rows;
  }
</script>

{#if result}
  {@const status = result.status ?? "unknown"}
  <div class="col" style="gap:12px">
    <div class="row">
      <Badge label={statusLabel(status)} tone={jobStatusTone(status)} />
      <span class="small muted">{statusNote(status)}</span>
      <span class="spacer"></span>
      {#if result.finished_at}
        <span class="small faint mono">{formatDate(result.finished_at)}</span>
      {/if}
    </div>

    {#if result.error}
      <div class="err-box">
        <div class="err-box__title">{t("common.error")}</div>
        <div>{result.error}</div>
      </div>
    {/if}

    {#if result.output_path}
      <div class="col" style="gap:6px">
        <span class="field__label">{t("protect.outputPath")}</span>
        <div class="row nowrap">
          <span class="mono truncate" title={result.output_path}>{result.output_path}</span>
        </div>
        <div class="btn-row">
          <button class="btn btn--sm" onclick={() => result && openFolder(result.output_path)}>
            {t("common.openFolder")}
          </button>
          {#if onverify}
            <button class="btn btn--sm" onclick={() => result?.output_path && onverify(result.output_path)}>
              {t("protect.verifyOutput")}
            </button>
          {/if}
          {#if onexport}
            <button class="btn btn--sm btn--ghost" onclick={() => result && onexport(result)}>
              {t("protect.exportReport")}
            </button>
          {/if}
        </div>
      </div>
    {/if}

    {#if result.output_partial_path}
      <div class="warn-box">
        <div class="warn-box__title">{t("status.partial")}</div>
        <div class="mono small break-all">{result.output_partial_path}</div>
      </div>
    {/if}

    <div class="grid grid-2" style="gap:12px">
      <div class="col" style="gap:6px">
        <span class="field__label">{t("protect.requested")}</span>
        <div class="chip-list">
          {#each result.requested ?? [] as item}
            <span class="chip">{item}</span>
          {:else}
            <span class="small faint">{t("common.none")}</span>
          {/each}
        </div>
      </div>

      <div class="col" style="gap:6px">
        <span class="field__label">{t("protect.executed")}</span>
        <div class="col" style="gap:3px">
          {#each result.executed ?? [] as item}
            <div class="row gap-sm small">
              <span class="dot dot--ok"></span>
              <span class="mono">{item.engine}.{item.action}</span>
              {#if item.version}<span class="faint">{item.version}</span>{/if}
            </div>
          {:else}
            <span class="small faint">{t("common.none")}</span>
          {/each}
        </div>
      </div>

      <div class="col" style="gap:6px">
        <span class="field__label">{t("protect.verified")}</span>
        <div class="col" style="gap:3px">
          {#each result.verified ?? [] as item}
            <div class="row gap-sm small">
              <span class="dot {item.ok ? 'dot--ok' : 'dot--err'}"></span>
              <span class="mono">{item.check}</span>
              <span class="faint">{item.ok ? "ok" : "ko"}</span>
            </div>
          {:else}
            <span class="small faint">{t("common.none")}</span>
          {/each}
        </div>
      </div>

      <div class="col" style="gap:6px">
        <span class="field__label">{t("protect.skipped")} / {t("protect.failed")}</span>
        <div class="col" style="gap:3px">
          {#each result.skipped ?? [] as item}
            <div class="row gap-sm small">
              <span class="dot dot--warn"></span>
              <span class="mono">{item.engine}</span>
              <span class="faint truncate">{item.reason ?? ""}</span>
            </div>
          {/each}
          {#each result.failed ?? [] as item}
            <div class="row gap-sm small">
              <span class="dot dot--err"></span>
              <span class="mono">{item.engine}</span>
              <span class="faint truncate">{item.error ?? ""}{item.required ? " (required)" : ""}</span>
            </div>
          {/each}
          {#if (result.skipped?.length ?? 0) === 0 && (result.failed?.length ?? 0) === 0}
            <span class="small faint">{t("common.none")}</span>
          {/if}
        </div>
      </div>
    </div>

    {#if result.hashes}
      <div class="col" style="gap:6px">
        <span class="field__label">{t("protect.hashes")}</span>
        <dl class="kv">
          <dt>{t("protect.inputHash")}</dt>
          <dd class="mono small break-all" title={result.hashes.input_sha256 ?? ""}>
            {result.hashes.input_sha256 ?? "—"}
          </dd>
          <dt>{t("protect.outputHash")}</dt>
          <dd class="mono small break-all" title={result.hashes.output_sha256 ?? ""}>
            {result.hashes.output_sha256 ?? "—"}
          </dd>
          {#if result.receipt_id}
            <dt>{t("protect.receiptId")}</dt>
            <dd class="mono small break-all">{result.receipt_id}</dd>
          {/if}
        </dl>
      </div>
    {/if}

    {#if result.quality?.output}
      <div class="col" style="gap:6px">
        <span class="field__label">{t("protect.qualityOutput")}</span>
        <div class="table-wrap">
          <table class="table">
            <tbody>
              {#each qualityRows(t("protect.qualityOutput"), result.quality.output as Record<string, unknown>) as [name, value]}
                <tr>
                  <td class="muted">{name}</td>
                  <td class="mono">{value}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}

    {#if result.warnings && result.warnings.length > 0}
      <div class="warn-box">
        <div class="warn-box__title">{t("protect.warnings")}</div>
        <ul class="small" style="margin:0;padding-left:18px">
          {#each result.warnings as warning}
            <li>{warning}</li>
          {/each}
        </ul>
      </div>
    {/if}

    <div class="small faint mono">
      {t("common.status")}: {basename(result.output_path)} · job {shortHash(result.job_id, 12)} ·
      {formatNumber(result.executed?.length ?? 0, 0)} {t("protect.executed")}
    </div>
  </div>
{/if}
