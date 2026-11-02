<script lang="ts">
  import { onMount } from "svelte";
  import logoUrl from "../../../../../assets/LogoMio.svg";
  import { t } from "../i18n.svelte";
  import { engine, loadHost, connect, checkUpdates } from "../engine.svelte";
  import { engineApi } from "../ipc";
  import { toast } from "../ui.svelte";
  import { formatDate } from "../format";
  import Icon from "../components/Icon.svelte";

  const repo = "Nicotina4ever/NL-VO-SHIELD";
  const repoUrl = `https://github.com/${repo}`;
  const siteUrl = "https://www.nicolalanci.com";
  const mailUrl = "mailto:info@nicolalanci.com";

  async function openUrl(url: string): Promise<void> {
    try {
      await engineApi.openPath(url, "file");
    } catch {
      toast("err", t("protect.openFailed"));
    }
  }

  onMount(() => {
    void (async () => {
      if (!engine.host) await loadHost();
      if (engine.conn !== "ready") await connect();
      if (engine.settings?.host?.autoUpdateCheck) void checkUpdates(false);
    })();
  });
</script>

<div class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("about.title")}</h1>
      <p>{t("about.subtitle")}</p>
    </div>
  </div>

  <div class="card">
    <div class="card__body">
      <div class="row" style="gap:18px;align-items:center">
        <img
          src={logoUrl}
          alt="NL VO Shield"
          style="width:96px;height:96px;border-radius:16px;background:var(--nl-surface-2);border:1px solid var(--nl-border);object-fit:contain"
        />
        <div class="stack" style="gap:4px">
          <h2 style="font-size:20px">{t("app.name")}</h2>
          <div class="small muted">{t("app.tagline")}</div>
          <div class="row gap-sm small">
            <span class="chip">{t("about.appVersion")} {engine.host?.appVersion ?? "0.1.0"}</span>
            <span class="chip">{t("about.engineVersion")} {engine.health?.version ?? "—"}</span>
          </div>
          <div class="small faint">{t("about.brandNote")}</div>
        </div>
        <span class="spacer"></span>
        <button class="btn" onclick={() => void openUrl(repoUrl)}>
          <Icon name="external" size={14} />
          {t("about.openRepo")}
        </button>
      </div>
    </div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <div class="card__head"><h2>{t("about.buildInfo")}</h2></div>
      <div class="card__body">
        <dl class="kv">
          <dt>{t("about.appVersion")}</dt>
          <dd class="mono">{engine.host?.appVersion ?? "—"}</dd>
          <dt>{t("about.engineVersion")}</dt>
          <dd class="mono">{engine.health?.version ?? "—"}</dd>
          <dt>{t("dashboard.platform")}</dt>
          <dd class="mono">{engine.host?.os ?? "—"} / {engine.host?.arch ?? "—"}</dd>
          <dt>{t("diagnostics.pythonPath")}</dt>
          <dd class="mono small break-all">{engine.host?.python?.path ?? t("common.notAvailable")}</dd>
          <dt>{t("dashboard.dataDir")}</dt>
          <dd class="mono small break-all">{engine.host?.paths.dataDir ?? "—"}</dd>
          <dt>{t("about.repo")}</dt>
          <dd class="mono small break-all">{repo}</dd>
        </dl>
        {#if !engine.online}
          <div class="warn-box" style="margin-top:10px">
            <div class="warn-box__title">{t("about.engineMissing")}</div>
            <div class="small">{t("about.engineMissingHint")}</div>
          </div>
        {/if}
      </div>
    </div>

    <div class="card">
      <div class="card__head">
        <h2>{t("about.updates")}</h2>
        <span class="spacer"></span>
        <button class="btn btn--ghost btn--sm" onclick={() => void checkUpdates(true)}>
          <Icon name="refresh" size={14} />
          {t("settings.checkUpdates")}
        </button>
      </div>
      <div class="card__body">
        <dl class="kv">
          <dt>{t("about.updaterStatus")}</dt>
          <dd>{t("about.updaterAutoOff")}</dd>
          <dt>{t("settings.currentVersion")}</dt>
          <dd class="mono">{engine.update?.currentVersion ?? engine.host?.appVersion ?? "—"}</dd>
          <dt>{t("settings.latestVersion")}</dt>
          <dd class="mono">{engine.update?.latestVersion ?? "—"}</dd>
          <dt>{t("settings.lastChecked")}</dt>
          <dd class="mono small">{formatDate(engine.update?.checkedAt ?? null)}</dd>
        </dl>
        <div class="hint-box small" style="margin-top:10px">
          {t("about.updaterManualOn")} · {t("about.offlineText")}
        </div>
        {#if engine.update?.releaseUrl}
          <div class="btn-row" style="margin-top:10px">
            <button class="btn btn--sm" onclick={() => engine.update?.releaseUrl && void openUrl(engine.update.releaseUrl)}>
              <Icon name="external" size={13} />
              {t("settings.openRelease")}
            </button>
          </div>
        {/if}
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__head"><h2>{t("about.owner")}</h2></div>
    <div class="card__body">
      <div class="row" style="gap:18px;align-items:flex-start;flex-wrap:wrap">
        <div class="stack" style="gap:2px">
          <span class="bold" style="font-size:15px">{t("about.ownerName")}</span>
          <span class="small muted">{t("about.brandMeaning")}</span>
        </div>
        <span class="spacer"></span>
        <div class="col" style="gap:6px">
          <button class="btn btn--sm" onclick={() => void openUrl(siteUrl)}>
            <Icon name="external" size={13} />
            {t("about.site")}
          </button>
          <button class="btn btn--sm btn--ghost" onclick={() => void openUrl(mailUrl)}>
            {t("about.email")}
          </button>
        </div>
      </div>
      <p class="small muted" style="margin-bottom:0;margin-top:12px">{t("about.copyright")}</p>
      <p class="small faint" style="margin-bottom:0">
        {t("about.repoNote")} ·
        <button class="btn btn--ghost btn--sm" onclick={() => void openUrl(repoUrl)}>{repo}</button>
      </p>
    </div>
  </div>

  <div class="card">
    <div class="card__head"><h2>{t("about.limitations")}</h2></div>
    <div class="card__body">
      <p class="small muted" style="margin-top:0">{t("about.limitationsIntro")}</p>
      <ul class="col small" style="gap:8px;padding-left:18px;margin:0">
        <li>{t("about.limitWatermark")}</li>
        <li>{t("about.limitAnticlone")}</li>
        <li>{t("about.limitC2pa")}</li>
        <li>{t("about.limitFingerprint")}</li>
        <li>{t("about.limitModels")}</li>
        <li>{t("about.limitOffline")}</li>
      </ul>
    </div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <div class="card__head"><h2>{t("about.privacy")}</h2></div>
      <div class="card__body">
        <p class="small" style="margin:0">{t("about.privacyText")}</p>
      </div>
    </div>
    <div class="card">
      <div class="card__head"><h2>{t("about.credits")}</h2></div>
      <div class="card__body">
        <p class="small" style="margin:0">{t("about.creditsText")}</p>
        <p class="small faint" style="margin-bottom:0">{t("about.license")}: MIT (codice del progetto)</p>
      </div>
    </div>
  </div>
</div>
