<script lang="ts">
  import { ui } from "../ui.svelte";
  import { onMount } from "svelte";
  import { t } from "../i18n.svelte";
  import { toast } from "../ui.svelte";
  import { refreshEngines } from "../engine.svelte";
  import { componentsApi, componentsJobsApi, micromambaApi, type ComponentInfo, type MicromambaStatus } from "../ipc";
  import { formatBytes } from "../format";

  let items = $state<ComponentInfo[]>([]);
  let total = $state(0);
  let busy = $state<string | null>(null);
  let paths = $state<Record<string, string>>({});
  let mm = $state<MicromambaStatus | null>(null);
  let envName = $state("e2e");
  let mmBusy = $state(false);
  let progress = $state<{ id: string; pct: number; msg: string } | null>(null);

  async function refresh() {
    try {
      const res = await componentsApi.list();
      items = res.items;
      total = res.total_bytes;
    } catch (err) {
      toast("err", t("common.unknownError"), String((err as Error)?.message ?? err));
    }
  }

  async function refreshMm() {
    try { mm = (await micromambaApi.status()).micromamba; } catch { /* backend older */ }
  }

  async function act(item: ComponentInfo, action: string) {
    if (action === "uninstall" && !confirm(t("components.confirm"))) return;
    const local = paths[item.id]?.trim() || null;
    if (action === "install" && !local && !item.bootstrap) {
      toast("err", t("components.install"), t("components.requirements"));
      return;
    }
    busy = item.id;
    try {
      const result = await componentsApi.action({ id: item.id, action, localPath: local, confirmed: action === "install" || action === "uninstall" });
      if (result && (result as any).job_id) {
        const jobId = String((result as any).job_id);
        for (;;) {
          await new Promise((r) => setTimeout(r, 1200));
          const { job } = await componentsJobsApi.job(jobId);
          progress = { id: item.id, pct: Number(job.percent ?? 0), msg: String(job.message ?? job.stage ?? "") };
          if (job.status === "done") { toast("ok", item.label, action); break; }
          if (job.status === "error") { toast("err", item.label, String(job.message ?? "errore")); break; }
        }
        progress = null;
      } else {
        toast("ok", item.label, action);
      }
      await refresh();
      await refreshEngines();
    } catch (err) {
      toast("err", item.label, String((err as Error)?.message ?? err));
    } finally {
      busy = null;
    }
  }

  async function mmAct(action: "install" | "verify" | "uninstall" | "create" | "remove") {
    mmBusy = true;
    try {
      if (action === "install") await micromambaApi.install();
      else if (action === "verify") await micromambaApi.verify();
      else if (action === "uninstall") { if (!confirm(t("components.confirm"))) return; await micromambaApi.uninstall(); }
      else if (action === "create") await micromambaApi.createEnv(envName || "e2e");
      else { if (!confirm(t("components.confirm"))) return; await micromambaApi.removeEnv(envName); }
      await refreshMm();
      await refreshEngines();
      toast("ok", t("micromamba.title"), action);
    } catch (err) {
      toast("err", t("micromamba.title"), String((err as Error)?.message ?? err));
    } finally {
      mmBusy = false;
    }
  }

  async function installAll(): Promise<void> {
    const external = items.filter((item) => !item.builtin && item.actions.includes("install"));
    for (const item of external) {
      paths[item.id] = "";
      await act(item, "install");
    }
  }

  onMount(() => { void refresh(); void refreshMm(); });
</script>

<div class="page mode-{ui.viewMode}">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("components.title")}</h1>
      <p>{t("components.subtitle")}</p>
    </div>
    <div class="row gap-sm">
      <button class="btn btn--primary" disabled={busy !== null} onclick={() => void installAll()}>{t("components.installAll")}</button>
      <span class="badge">{formatBytes(total)}</span>
    </div>
  </div>

  <section class="card" style="margin-bottom:14px">
    <div class="card__head">
      <div><h2>{t("micromamba.title")}</h2><p class="muted">{mm ? `${mm.platform} · ${mm.version} · ${formatBytes(mm.size_bytes)}` : "—"}</p></div>
      <span class="badge" class:tone-good={mm?.installed} class:tone-warn={!mm?.installed}>{mm?.installed ? t("micromamba.installed") : t("micromamba.notInstalled")}</span>
    </div>
    <div class="card__body col" style="gap:10px">
      <div class="row gap-sm" style="flex-wrap:wrap">
        <button class="btn btn--primary" disabled={mmBusy} onclick={() => mmAct("install")}>{t("micromamba.install")}</button>
        <button class="btn" disabled={mmBusy} onclick={() => mmAct("verify")}>{t("micromamba.verify")}</button>
        <button class="btn btn--danger" disabled={mmBusy} onclick={() => mmAct("uninstall")}>{t("micromamba.remove")}</button>
      </div>
      {#if mm?.installed}
        <div class="row gap-sm" style="flex-wrap:wrap">
          <input class="input" bind:value={envName} placeholder={t("micromamba.envName")} />
          <button class="btn" disabled={mmBusy} onclick={() => mmAct("create")}>{t("micromamba.create")}</button>
          <button class="btn" disabled={mmBusy} onclick={() => mmAct("remove")}>{t("micromamba.removeEnv")}</button>
        </div>
        {#each mm.envs as env (env.path)}
          <div class="row small"><span class="mono">{env.name}</span><span class="spacer"></span><span>{formatBytes(env.size_bytes)}</span></div>
        {/each}
      {/if}
    </div>
  </section>

  <div class="grid">
    {#each items as item (item.id)}
      <section class="card" aria-busy={busy === item.id}>
        <div class="card__head">
          <div><h2>{item.label}</h2><p class="muted">{item.description}</p></div>
          <span class="badge" class:tone-good={item.installed} class:tone-warn={!item.installed}>{item.installed ? t("components.installed") : t("components.notInstalled")}</span>
        </div>
        <div class="meta">
          <span>{t("components.threats")}: {item.threats.join(" / ")}</span>
          <span>{t("components.requirements")}: {item.requirements}</span>
          <span>{t("components.size")}: {formatBytes(item.size_bytes)}</span>
          {#if item.builtin}<span>{t("components.builtin")}</span>{/if}
          <span>{item.enabled ? t("components.enable") : t("components.disable")}</span>
        </div>
        {#if !item.installed && !item.builtin && !item.bootstrap}
          <label class="field"><span>{t("components.install")}</span><input bind:value={paths[item.id]} placeholder="C:\\percorso\\componente" /></label>
        {/if}
        {#if item.actions.includes("import_checkpoint")}
          <label class="field"><span>pretrained_ljs.pth</span><input bind:value={paths[item.id]} placeholder="C:\\percorso\\pretrained_ljs.pth" /></label>
        {/if}
        {#if progress && progress.id === item.id}
          <div class="small muted">{progress.pct}% · {progress.msg}</div>
          <progress max="100" value={progress.pct} style="width:100%"></progress>
        {/if}
        <div class="actions">
          {#if item.actions.includes("install")}<button class="btn btn--primary" disabled={busy === item.id} onclick={() => act(item, "install")}>{item.bootstrap ? t("components.installAuto") : t("components.install")}</button>{/if}
          {#if item.actions.includes("verify")}<button class="btn" disabled={busy === item.id} onclick={() => act(item, "verify")}>{t("components.verify")}</button>{/if}
          {#if item.actions.includes("enable")}<button class="btn" disabled={busy === item.id} onclick={() => act(item, "enable")}>{t("components.enable")}</button>{/if}
          {#if item.actions.includes("disable")}<button class="btn" disabled={busy === item.id} onclick={() => act(item, "disable")}>{t("components.disable")}</button>{/if}
          {#if item.actions.includes("import_checkpoint")}<button class="btn" disabled={busy === item.id} onclick={() => act(item, "import_checkpoint")}>{t("components.install")}</button>{/if}
          {#if item.actions.includes("uninstall")}<button class="btn btn--danger" disabled={busy === item.id} onclick={() => act(item, "uninstall")}>{t("components.uninstall")}</button>{/if}
        </div>
      </section>
    {/each}
  </div>
</div>

