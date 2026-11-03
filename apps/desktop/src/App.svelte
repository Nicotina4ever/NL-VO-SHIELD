<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import logoUrl from "../../../assets/LogoMio.svg";
  import { t, i18n, hostErrorMessage } from "./lib/i18n.svelte";
  import { ui, navigate, requestOpenFile, setLanguage, setTheme, setViewMode, setConsoleOpen, toggleConsole, toast } from "./lib/ui.svelte";
  import {
    bootEngine,
    connect,
    engine,
    loadModels,
    saveSettings,
  } from "./lib/engine.svelte";
  import { pickDirectory } from "./lib/dialogs";
  import { single, batch, cancelBatchJob, cancelSingleJob } from "./lib/run.svelte";
  import { attachAllListeners, detachAllListeners } from "./lib/events.svelte";
  import { isTauri } from "./lib/ipc";
  import Icon from "./lib/components/Icon.svelte";
  import Toasts from "./lib/components/Toasts.svelte";
  import ConsoleDrawer from "./lib/components/ConsoleDrawer.svelte";
  import Modal from "./lib/components/Modal.svelte";

  import Dashboard from "./lib/pages/Dashboard.svelte";
  import Protect from "./lib/pages/Protect.svelte";
  import Batch from "./lib/pages/Batch.svelte";
  import Verify from "./lib/pages/Verify.svelte";
import Laboratorio from "./lib/pages/Laboratorio.svelte";
  import Engines from "./lib/pages/Engines.svelte";
  import Profiles from "./lib/pages/Profiles.svelte";
  import Settings from "./lib/pages/Settings.svelte";
  import Diagnostics from "./lib/pages/Diagnostics.svelte";
  import Componenti from "./lib/pages/Componenti.svelte";
  import About from "./lib/pages/About.svelte";

  const appVersion = $derived(engine.host?.appVersion ?? "0.1.0");
  const engineLabel = $derived(
    engine.conn === "ready"
      ? t("dashboard.engineOnline")
      : engine.conn === "starting" || engine.booting
        ? t("dashboard.engineStarting")
        : t("dashboard.engineOffline"),
  );
  const engineTone = $derived(
    engine.conn === "ready" ? "dot--ok" : engine.conn === "starting" || engine.booting ? "dot--warn" : "dot--err",
  );
  const needsDataDirModal = $derived(
    Boolean(engine.host?.paths.needsDataDir) && !ui.dataDirDismissed,
  );
  let choosingDataDir = $state(false);

  async function chooseDataDir(): Promise<void> {
    const folder = await pickDirectory(t("settings.dataDir"));
    if (!folder) return;
    choosingDataDir = true;
    try {
      await saveSettings({}, { dataDir: folder });
      ui.dataDirDismissed = false;
      toast("ok", t("datadir.saved"), folder);
      await bootEngine();
    } catch (err) {
      toast(
        "err",
        t("settings.saveFailed"),
        hostErrorMessage(err as { code?: string; message?: string }),
      );
    } finally {
      choosingDataDir = false;
    }
  }

  async function useInstallDataDir(): Promise<void> {
    choosingDataDir = true;
    try {
      await saveSettings({}, { dataDir: null });
      ui.dataDirDismissed = false;
      await bootEngine();
    } catch (err) {
      toast(
        "err",
        t("settings.saveFailed"),
        hostErrorMessage(err as { code?: string; message?: string }),
      );
    } finally {
      choosingDataDir = false;
    }
  }

  const navGroups = [
    {
      label: "nav.groupMain",
      items: [
        { page: "dashboard", icon: "dashboard", key: "nav.dashboard" },
        { page: "protect", icon: "shield", key: "nav.protect" },
        { page: "batch", icon: "batch", key: "nav.batch" },
        { page: "verify", icon: "verify", key: "nav.verify" },
        { page: "lab", icon: "lab", key: "nav.lab" },
      ],
    },
    {
      label: "nav.groupSystem",
      items: [
        { page: "engines", icon: "engines", key: "nav.engines" },
        { page: "components", icon: "engines", key: "nav.components" },
        { page: "profiles", icon: "profiles", key: "nav.profiles" },
        { page: "settings", icon: "settings", key: "nav.settings" },
        { page: "diagnostics", icon: "diagnostics", key: "nav.diagnostics" },
        { page: "about", icon: "about", key: "nav.about" },
      ],
    },
  ] as const;

  function handleKeydown(event: KeyboardEvent): void {
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === "o") {
      event.preventDefault();
      requestOpenFile();
      return;
    }
    if (mod && event.key === "Enter") {
      event.preventDefault();
      if (ui.modalDepth === 0) ui.startRequest += 1;
      return;
    }
    if (mod && event.key === "`") {
      event.preventDefault();
      toggleConsole();
      return;
    }
    if (event.key === "F1" || (mod && event.key === "/")) {
      event.preventDefault();
      ui.shortcutsOpen = !ui.shortcutsOpen;
      return;
    }
    if (event.key === "Escape") {
      if (ui.consoleOpen) {
        setConsoleOpen(false);
        return;
      }
      if (ui.shortcutsOpen) {
        ui.shortcutsOpen = false;
        return;
      }
      if (ui.modalDepth > 0) return;
      if (single.running) {
        void cancelSingleJob();
        return;
      }
      if (batch.running) {
        void cancelBatchJob();
      }
    }
  }

  async function reconnect(): Promise<void> {
    await connect();
  }

  onMount(() => {
    void (async () => {
      await attachAllListeners();
      await bootEngine();
      const host = engine.host;
      if (host) {
        if ((host.language === "it" || host.language === "en") && host.language !== i18n.lang) {
          setLanguage(host.language);
        }
        if ((host.theme === "dark" || host.theme === "light") && host.theme !== ui.theme) {
          setTheme(host.theme);
        }
      }
      void loadModels().catch(() => undefined);
    })();
  });

  onDestroy(() => {
    detachAllListeners();
  });
</script>

<div class="app">
  <aside class="sidebar">
    <div class="side-brand">
      <img class="side-brand__logo" src={logoUrl} alt="NL VO Shield" />
      <div class="side-brand__text stack" style="gap:0">
        <span class="side-brand__name">{t("app.name")}</span>
        <span class="side-brand__tag">{t("app.tagline")}</span>
      </div>
    </div>

    <nav class="side-nav" aria-label={t("app.name")}>
      {#each navGroups as group}
        <div class="side-nav__group">{t(group.label)}</div>
        {#each group.items as item}
          <button
            class="nav-item {ui.page === item.page ? 'active' : ''}"
            onclick={() => navigate(item.page)}
            aria-current={ui.page === item.page ? "page" : undefined}
            title={t(item.key)}
          >
            <Icon name={item.icon} />
            <span class="nav-item__label">{t(item.key)}</span>
          </button>
        {/each}
      {/each}
    </nav>

    <div class="side-foot">
      <div class="row gap-sm side-foot__text">
        <span class="dot {engineTone}"></span>
        <span class="truncate">{engineLabel}</span>
      </div>
      <div class="row gap-sm side-foot__text">
        <span class="faint">{t("about.version")} {appVersion}</span>
        <span class="spacer"></span>
        <button
          class="btn btn--ghost btn--sm"
          onclick={() => (ui.shortcutsOpen = true)}
          title={t("shortcuts.title")}
          aria-label={t("shortcuts.title")}
        >
          ⌨
        </button>
      </div>
    </div>
  </aside>

  <div class="main">
    <header class="topbar">
      <span class="topbar__title">{t(`nav.${ui.page}`)}</span>
      <span class="topbar__sub mono">{t("app.name")}</span>
      <span class="topbar__spacer"></span>

      <button
        class="pill-bool"
        onclick={() => navigate("diagnostics")}
        title={engineLabel}
        aria-label={engineLabel}
      >
        <span class="dot {engineTone}"></span>
        <span class="small muted truncate" style="max-width:220px">{engineLabel}</span>
      </button>

      <button
        class="btn btn--ghost btn--sm"
        onclick={() => void reconnect()}
        disabled={engine.conn === "starting"}
        title={t("dashboard.refreshEngine")}
        aria-label={t("dashboard.refreshEngine")}
      >
        <Icon name="refresh" size={15} />
      </button>

      <button
        class="btn btn--ghost btn--sm"
        onclick={() => toggleConsole()}
        aria-pressed={ui.consoleOpen}
        title={t("console.open")}
        aria-label={t("console.open")}
      >
        <Icon name="terminal" size={15} />
      </button>

      <div class="seg" role="group" aria-label={t("settings.language")}>
        <button
          class="seg__item {i18n.lang === 'it' ? 'active' : ''}"
          onclick={() => setLanguage("it")}
          aria-pressed={i18n.lang === "it"}
        >
          IT
        </button>
        <button
          class="seg__item {i18n.lang === 'en' ? 'active' : ''}"
          onclick={() => setLanguage("en")}
          aria-pressed={i18n.lang === "en"}
        >
          EN
        </button>
      </div>

      <button
        class="btn btn--ghost btn--sm"
        onclick={() => setTheme(ui.theme === "dark" ? "light" : "dark")}
        title={t("settings.theme")}
        aria-label={t("settings.theme")}
      >
        {ui.theme === "dark" ? "☾" : "☀"}
      </button>
      <div class="seg" role="group" aria-label="Modalità">
        {#each ["simple", "expert", "advanced"] as mode}
          <button class="seg__item" class:active={ui.viewMode === mode}
            onclick={() => setViewMode(mode as "simple" | "expert" | "advanced")}
            aria-pressed={ui.viewMode === mode}>{t(`mode.${mode}`)}</button>
        {/each}
      </div>

    </header>

    <main class="content" tabindex="-1">
      {#if !isTauri()}
        <div class="warn-box" style="margin-bottom:14px">
          <div class="warn-box__title">{t("common.notTauri")}</div>
          <div>{t("common.notTauriHint")}</div>
        </div>
      {/if}

      {#if ui.page === "dashboard"}
        <Dashboard />
      {:else if ui.page === "protect"}
        <Protect />
      {:else if ui.page === "batch"}
        <Batch />
      {:else if ui.page === "verify"}
        <Verify />
      {:else if ui.page === "lab"}
        <Laboratorio />
      {:else if ui.page === "engines"}
        <Engines />
      {:else if ui.page === "components"}
        <Componenti />
      {:else if ui.page === "profiles"}
        <Profiles />
      {:else if ui.page === "settings"}
        <Settings />
      {:else if ui.page === "diagnostics"}
        <Diagnostics />
      {:else if ui.page === "about"}
        <About />
      {/if}
    </main>
  </div>
</div>

<Toasts />
<ConsoleDrawer open={ui.consoleOpen} />

{#if needsDataDirModal}
  <Modal title={t("datadir.title")} onclose={() => (ui.dataDirDismissed = true)}>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (ui.dataDirDismissed = true)}>
        {t("datadir.later")}
      </button>
      {#if engine.host?.paths.installWritable}
        <button class="btn" onclick={() => void useInstallDataDir()} disabled={choosingDataDir}>
          {t("settings.dataDirReset")}
        </button>
      {/if}
      <button class="btn btn--primary" onclick={() => void chooseDataDir()} disabled={choosingDataDir}>
        {#if choosingDataDir}<span class="spinner"></span>{/if}
        {t("datadir.choose")}
      </button>
    {/snippet}
    <div class="col" style="gap:12px">
      <p style="margin:0">{t("datadir.body")}</p>
      <dl class="kv">
        <dt>{t("datadir.installDir")}</dt>
        <dd class="mono small break-all">{engine.host?.paths.installDir ?? "—"}</dd>
        <dt>{t("datadir.suggested")}</dt>
        <dd class="mono small break-all">{engine.host?.paths.suggestedDataDir ?? "—"}</dd>
        {#if engine.host?.paths.dataDir}
          <dt>{t("datadir.current")}</dt>
          <dd class="mono small break-all">{engine.host.paths.dataDir}</dd>
        {/if}
      </dl>
      <div class="warn-box small">{t("datadir.required")}</div>
    </div>
  </Modal>
{/if}

<svelte:window onkeydown={handleKeydown} />

{#if ui.shortcutsOpen}
  <Modal title={t("shortcuts.title")} onclose={() => (ui.shortcutsOpen = false)}>
    <table class="table">
      <tbody>
        <tr>
          <td class="mono">Ctrl + O</td>
          <td>{t("shortcuts.open")}</td>
        </tr>
        <tr>
          <td class="mono">Ctrl + Enter</td>
          <td>{t("shortcuts.start")}</td>
        </tr>
        <tr>
          <td class="mono">Esc</td>
          <td>{t("shortcuts.cancel")}</td>
        </tr>
        <tr>
          <td class="mono">F1 / Ctrl + /</td>
          <td>{t("shortcuts.help")}</td>
        </tr>
      </tbody>
    </table>
    <p class="small muted" style="margin-bottom:0">
      {t("shortcuts.open")} · {t("shortcuts.start")} · {t("shortcuts.cancel")}
    </p>
  </Modal>
{/if}
