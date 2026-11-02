<script lang="ts">
  import { onMount } from "svelte";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import { ui, toast } from "../ui.svelte";
  import { engine, loadReceipts, refreshEngines } from "../engine.svelte";
  import { engineApi } from "../ipc";
  import { pickAudioFile, pickSavePath } from "../dialogs";
  import { verifyOutcomeTone, formatNumber, formatDate, basename } from "../format";
  import type { Receipt, VerifyResult } from "../types";
  import Icon from "../components/Icon.svelte";
  import Badge from "../components/Badge.svelte";
  import Waveform from "../components/Waveform.svelte";

  let path = $state("");
  let verifying = $state(false);
  let result = $state<VerifyResult | null>(null);
  let error = $state<string | null>(null);
  let quality = $state<Record<string, unknown> | null>(null);
  let qualityLoading = $state(false);
  let qualityError = $state<string | null>(null);
  let receiptsError = $state<string | null>(null);
  let lastPrefill = $state<string | null>(null);

  const outcomeTone = $derived(verifyOutcomeTone(result?.outcome));
  const outcomeLabel = $derived.by(() => {
    const outcome = result?.outcome;
    if (outcome === "verified") return t("verify.verified");
    if (outcome === "partial") return t("verify.partial");
    if (outcome === "not_detected") return t("verify.notDetected");
    if (outcome === "indeterminate") return t("verify.indeterminate");
    return outcome ?? t("common.none");
  });

  function boolLabel(value: unknown): string {
    if (value === true) return t("common.yes");
    if (value === false) return t("common.no");
    return t("common.unknown");
  }

  function scalarRows(value: Record<string, unknown> | null): [string, string][] {
    if (!value) return [];
    const rows: [string, string][] = [];
    for (const [key, raw] of Object.entries(value)) {
      if (raw == null || typeof raw === "object") continue;
      rows.push([key, typeof raw === "number" ? formatNumber(raw, 3) : String(raw)]);
    }
    return rows;
  }

  async function pickFile(): Promise<void> {
    const paths = await pickAudioFile({ title: t("verify.title") });
    if (paths && paths.length > 0) {
      path = paths[0];
      result = null;
      quality = null;
      error = null;
    }
  }

  async function runVerify(): Promise<void> {
    if (!path) {
      toast("warn", t("protect.chooseInputFirst"));
      return;
    }
    verifying = true;
    error = null;
    try {
      result = await engineApi.verifyFile(path);
    } catch (err) {
      error = hostErrorMessage(err as { code?: string; message?: string });
      result = null;
    } finally {
      verifying = false;
    }
  }

  async function runQuality(): Promise<void> {
    if (!path) return;
    qualityLoading = true;
    qualityError = null;
    try {
      quality = await engineApi.analyzeQuality(path);
    } catch (err) {
      qualityError = hostErrorMessage(err as { code?: string; message?: string });
      quality = null;
    } finally {
      qualityLoading = false;
    }
  }

  async function exportReport(): Promise<void> {
    if (!result) return;
    const defaultName = `nl-vo-shield-verify-${Date.now()}.json`;
    const target = await pickSavePath({
      title: t("verify.exportJson"),
      defaultPath: defaultName,
      filters: [{ name: "JSON", extensions: ["json"] }],
    });
    if (!target) return;
    try {
      const payload = { verification: result, quality };
      const response = await engineApi.exportReport(JSON.stringify(payload, null, 2), defaultName, target);
      if (response.saved) toast("ok", t("verify.reportSaved"), response.path ?? target);
    } catch (err) {
      toast("err", t("common.error"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  async function verifyReceipt(receipt: Receipt): Promise<void> {
    try {
      const response = await engineApi.verifyReceipt(receipt.id);
      if (response.valid) toast("ok", t("verify.receiptValid"), receipt.id);
      else toast("warn", t("verify.receipt"), response.reason ?? receipt.id);
    } catch (err) {
      toast("err", t("common.error"), hostErrorMessage(err as { code?: string; message?: string }));
    }
  }

  onMount(() => {
    void (async () => {
      if (engine.engines.length === 0) void refreshEngines();
      try {
        await loadReceipts();
      } catch (err) {
        receiptsError = hostErrorMessage(err as { code?: string; message?: string });
      }
    })();
  });

  $effect(() => {
    const prefill = ui.verifyPrefill;
    if (!prefill || prefill === lastPrefill) return;
    lastPrefill = prefill;
    path = prefill;
    result = null;
    quality = null;
    void runVerify();
  });
</script>

<div class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("verify.title")}</h1>
      <p>{t("verify.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      <button class="btn btn--sm" onclick={() => void pickFile()}>
        <Icon name="file" size={14} />
        {t("common.chooseFile")}
      </button>
    </div>
  </div>

  <div class="card">
    <div class="card__head">
      <h2>{t("protect.inputFile")}</h2>
      <span class="spacer"></span>
      {#if result}
        <Badge label={outcomeLabel} tone={outcomeTone} />
      {/if}
    </div>
    <div class="card__body">
      <div class="input-row">
        <input class="input mono" bind:value={path} placeholder={t("common.path")} />
        <button class="btn" onclick={() => void pickFile()}>{t("common.browse")}</button>
        <button class="btn btn--primary" onclick={() => void runVerify()} disabled={verifying || !path}>
          {#if verifying}<span class="spinner"></span>{/if}
          {t("verify.runVerify")}
        </button>
      </div>
      {#if path}
        <div style="margin-top:12px">
          <Waveform path={path} />
        </div>
      {/if}
      {#if error}
        <div class="err-box" style="margin-top:12px">
          <div class="err-box__title">{t("verify.verifiedFailed")}</div>
          <div>{error}</div>
        </div>
      {/if}
      {#if result?.warnings && result.warnings.length > 0}
        <div class="warn-box" style="margin-top:12px">
          <div class="warn-box__title">{t("verify.warnings")}</div>
          <ul class="small" style="margin:0;padding-left:18px">
            {#each result.warnings as warning}
              <li>{warning}</li>
            {/each}
          </ul>
        </div>
      {/if}
    </div>
  </div>

  {#if result}
    <div class="grid grid-2">
      <div class="card">
        <div class="card__head">
          <h2>{t("verify.watermark")}</h2>
          <span class="spacer"></span>
          <Badge
            label={result.watermark?.detected ? t("verify.watermarkDetected") : t("verify.notDetected")}
            tone={result.watermark?.detected ? "ok" : "muted"}
          />
        </div>
        <div class="card__body">
          <dl class="kv">
            <dt>{t("verify.confidence")}</dt>
            <dd class="mono">
              {result.watermark?.confidence != null
                ? formatNumber(result.watermark.confidence as number, 3)
                : "—"}
            </dd>
            <dt>{t("verify.payload")}</dt>
            <dd class="mono break-all">{result.watermark?.payload_hex ?? "—"}</dd>
            <dt>{t("engines.title")}</dt>
            <dd class="mono">{result.watermark?.engine ?? "—"}</dd>
          </dl>
          <p class="small faint" style="margin-bottom:0">{t("verify.note")}</p>
        </div>
      </div>

      <div class="card">
        <div class="card__head">
          <h2>{t("verify.c2pa")}</h2>
          <span class="spacer"></span>
          <Badge
            label={result.c2pa?.present ? t("common.yes") : t("verify.notDetected")}
            tone={result.c2pa?.valid ? "ok" : result.c2pa?.present ? "warn" : "muted"}
          />
        </div>
        <div class="card__body">
          <dl class="kv">
            <dt>{t("verify.c2paPresent")}</dt>
            <dd>{boolLabel(result.c2pa?.present)}</dd>
            <dt>{t("verify.c2paValid")}</dt>
            <dd>{boolLabel(result.c2pa?.valid)}</dd>
            <dt>{t("verify.c2paTrusted")}</dt>
            <dd>{boolLabel(result.c2pa?.trusted)}</dd>
            <dt>{t("verify.c2paSigner")}</dt>
            <dd class="mono small break-all">{result.c2pa?.signer ?? "—"}</dd>
          </dl>
          {#if result.c2pa?.claims && Object.keys(result.c2pa.claims).length > 0}
            <details style="margin-top:8px">
              <summary class="small muted" style="cursor:pointer">{t("verify.manifestClaims")}</summary>
              <pre class="mono small" style="overflow:auto;max-height:180px;margin:6px 0 0">
{JSON.stringify(result.c2pa.claims, null, 2)}</pre>
            </details>
          {/if}
        </div>
      </div>

      <div class="card">
        <div class="card__head">
          <h2>{t("verify.fingerprint")}</h2>
        </div>
        <div class="card__body">
          <dl class="kv">
            <dt>{t("verify.fingerprintMatched")}</dt>
            <dd>{boolLabel(result.fingerprint?.matched)}</dd>
            <dt>{t("verify.similarity")}</dt>
            <dd class="mono">
              {result.fingerprint?.similarity != null
                ? formatNumber(result.fingerprint.similarity as number, 3)
                : "—"}
            </dd>
            <dt>{t("verify.receiptId")}</dt>
            <dd class="mono small break-all">{result.fingerprint?.receipt_id ?? "—"}</dd>
          </dl>
        </div>
      </div>

      <div class="card">
        <div class="card__head">
          <h2>{t("verify.receipt")}</h2>
          <span class="spacer"></span>
          <Badge
            label={result.receipt?.found ? t("verify.receiptFound") : t("verify.notDetected")}
            tone={result.receipt?.valid ? "ok" : result.receipt?.found ? "warn" : "muted"}
          />
        </div>
        <div class="card__body">
          <dl class="kv">
            <dt>{t("verify.receiptFound")}</dt>
            <dd>{boolLabel(result.receipt?.found)}</dd>
            <dt>{t("verify.receiptValid")}</dt>
            <dd>{boolLabel(result.receipt?.valid)}</dd>
            <dt>{t("verify.receiptId")}</dt>
            <dd class="mono small break-all">{result.receipt?.id ?? "—"}</dd>
            <dt>{t("verify.sha256")}</dt>
            <dd class="mono small break-all">{result.sha256 ?? "—"}</dd>
          </dl>
        </div>
        <div class="card__foot">
          <button class="btn btn--sm" onclick={() => void exportReport()}>
            <Icon name="download" size={14} />
            {t("verify.exportJson")}
          </button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card__head">
        <h2>{t("verify.details")}</h2>
      </div>
      <div class="card__body">
        <details>
          <summary class="small muted" style="cursor:pointer">{t("common.details")}</summary>
          <pre class="mono small" style="overflow:auto;max-height:320px;margin:6px 0 0">
{JSON.stringify(result, null, 2)}</pre>
        </details>
      </div>
    </div>
  {:else}
    <div class="card">
      <div class="card__body">
        <div class="empty">
          <div class="bold">{t("verify.noResult")}</div>
          <div class="small" style="margin-top:4px">{t("verify.noResultHint")}</div>
        </div>
      </div>
    </div>
  {/if}

  <div class="grid grid-2">
    <div class="card">
      <div class="card__head">
        <h2>{t("verify.quality")}</h2>
        <span class="spacer"></span>
        <button class="btn btn--sm" onclick={() => void runQuality()} disabled={!path || qualityLoading}>
          {#if qualityLoading}<span class="spinner"></span>{/if}
          {t("verify.runQuality")}
        </button>
      </div>
      <div class="card__body">
        {#if qualityError}
          <div class="err-box">
            <div class="err-box__title">{t("verify.qualityFailed")}</div>
            <div>{qualityError}</div>
          </div>
        {:else if quality}
          <table class="table">
            <tbody>
              {#each scalarRows(quality) as [key, value]}
                <tr>
                  <td class="muted mono small">{key}</td>
                  <td class="mono small">{value}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <div class="empty">{t("verify.noQuality")}</div>
        {/if}
      </div>
    </div>

    <div class="card">
      <div class="card__head">
        <h2>{t("verify.receipt")}</h2>
        <span class="spacer"></span>
        <button class="btn btn--ghost btn--sm" onclick={() => void loadReceipts()}>
          <Icon name="refresh" size={14} />
          {t("common.refresh")}
        </button>
      </div>
      <div class="card__body card__body--flush">
        {#if receiptsError}
          <div class="empty">{receiptsError}</div>
        {:else if engine.receipts.length === 0}
          <div class="empty">{t("common.empty")}</div>
        {:else}
          <table class="table">
            <thead>
              <tr>
                <th>{t("verify.receiptId")}</th>
                <th>{t("common.date", {})}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {#each engine.receipts.slice(0, 12) as receipt (receipt.id)}
                <tr>
                  <td class="mono small truncate" style="max-width:220px" title={receipt.id}>
                    {receipt.id}
                  </td>
                  <td class="small faint mono">{formatDate(receipt.created_at)}</td>
                  <td>
                    <button class="btn btn--ghost btn--sm" onclick={() => void verifyReceipt(receipt)}>
                      {t("verify.receiptValid")}
                    </button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </div>
    </div>
  </div>

  {#if path}
    <div class="small faint mono">File: {basename(path)}</div>
  {/if}
</div>
