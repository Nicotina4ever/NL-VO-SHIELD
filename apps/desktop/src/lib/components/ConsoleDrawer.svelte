<script lang="ts">
  /**
   * Console in-app: drawer con i log del motore Python.
   *
   * - carica i log già esposti da `engine_logs` e li aggiorna in tempo reale
   *   dagli eventi `engine://log` (nessuna finestra cmd separata);
   * - auto-scroll, copia, pulisci vista, apri file log (snapshot su disco);
   * - stato aperto/chiuso persistito in localStorage (ui.consoleOpen).
   */
  import { onDestroy, onMount } from "svelte";
  import { ENGINE_EVENTS, engineApi, onEvent } from "../ipc";
  import type { EngineLogLine } from "../types";
  import { t } from "../i18n.svelte";
  import { toast, setConsoleOpen } from "../ui.svelte";
  import Icon from "./Icon.svelte";

  let { open = false }: { open?: boolean } = $props();

  let lines = $state<EngineLogLine[]>([]);
  let loading = $state(false);
  let autoScroll = $state(true);
  let listEl = $state<HTMLDivElement | undefined>(undefined);
  let unlisten: Array<() => void> = [];

  const AUTO_KEY = "nlvs.consoleAutoscroll";

  function pushLine(entry: EngineLogLine): void {
    lines = [...lines.slice(-1500), entry];
  }

  async function load(): Promise<void> {
    loading = true;
    try {
      const response = await engineApi.logs(600);
      lines = response.lines ?? [];
    } catch {
      /* il motore può non essere avviato: la console resta vuota e lo dice */
    } finally {
      loading = false;
    }
  }

  async function copyAll(): Promise<void> {
    try {
      const text = lines
        .map((entry) => `${entry.ts ?? ""} [${entry.level ?? "log"}] ${entry.line ?? ""}`)
        .join("\n");
      await navigator.clipboard.writeText(text);
      toast("ok", t("console.copied"));
    } catch {
      toast("err", t("console.copied"));
    }
  }

  async function openLogFile(): Promise<void> {
    try {
      const result = await engineApi.openEngineLog();
      if (!result.opened) toast("warn", t("console.openFailed"));
    } catch {
      toast("err", t("console.openFailed"));
    }
  }


  $effect(() => {
    if (open) {
      void load();
      try {
        autoScroll = localStorage.getItem(AUTO_KEY) !== "0";
      } catch {
        /* ignore */
      }
    }
  });

  $effect(() => {
    void autoScroll;
    void lines.length;
    if (autoScroll && listEl) listEl.scrollTop = listEl.scrollHeight;
  });

  onMount(() => {
    void (async () => {
      unlisten = [await onEvent<EngineLogLine>(ENGINE_EVENTS.log, (payload) => pushLine(payload))];
    })();
  });

  onDestroy(() => {
    for (const off of unlisten) off();
  });
</script>



{#if open}
  <aside class="console-drawer" aria-label={t("console.title")}>
    <header class="console-drawer__head">
      <Icon name="terminal" size={15} />
      <span class="console-drawer__title">{t("console.title")}</span>
      <span class="small faint">{t("console.subtitle")}</span>
      <span class="spacer"></span>
      <span class="chip tnum">{t("console.lineCount", { n: lines.length })}</span>
      <button
        class="btn btn--sm btn--ghost"
        onclick={() => {
          autoScroll = !autoScroll;
          try {
            localStorage.setItem(AUTO_KEY, autoScroll ? "1" : "0");
          } catch {
            /* ignore */
          }
        }}
        aria-pressed={autoScroll}
        title={t("console.autoScroll")}
      >
        {t("console.autoScroll")}
      </button>
      <button class="btn btn--sm" onclick={copyAll}>{t("console.copy")}</button>
      <button class="btn btn--sm" onclick={openLogFile} title={t("console.openFileHint")}>
        {t("console.openFile")}
      </button>
      <button class="btn btn--sm btn--ghost" onclick={() => (lines = [])}>
        {t("console.clear")}
      </button>
      <button
        class="icon-btn"
        onclick={() => setConsoleOpen(false)}
        aria-label={t("console.close")}
        title={t("console.close")}
      >
        <Icon name="close" size={15} />
      </button>
    </header>
    <div class="console-drawer__body" bind:this={listEl}>
      {#if lines.length === 0}
        <div class="empty">
          {loading ? t("console.autoScroll") + "…" : t("console.empty")}
        </div>
      {:else}
        <pre class="console-drawer__log">
{#each lines as entry}
<span class="log-line log-line--{entry.level ?? 'log'}">{entry.ts ?? ""} <span class="log-level">{entry.level ?? "log"}</span> {entry.line ?? ""}</span>
{/each}
        </pre>
      {/if}
    </div>
  </aside>
{/if}
