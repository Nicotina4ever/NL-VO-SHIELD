/** Stato UI globale: pagina corrente, tema, lingua, toast e richieste trasversali. */
import { i18n, setLang, type Lang } from "./i18n.svelte";

export type Page =
  | "dashboard"
  | "protect"
  | "batch"
  | "verify"
  | "lab"
  | "engines"
  | "components"
  | "profiles"
  | "settings"
  | "diagnostics"
  | "about";

export type Theme = "dark" | "light";
export type ToastKind = "info" | "ok" | "warn" | "err";

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  title?: string;
}

const THEME_KEY = "nlvs.theme";
const LANG_KEY = "nlvs.lang";
const CONSOLE_KEY = "nlvs.consoleOpen";
const TOAST_TTL = 7000;

export const ui = $state({
  page: "dashboard" as Page,
  theme: "dark" as Theme,
  booted: false,
  toasts: [] as Toast[],
  /** Incrementato per chiedere alla pagina Proteggi/Batch di aprire il selettore file. */
  openFileRequest: 0,
  /** Percorso da precompilare nella pagina Verifica (es. output di un job). */
  verifyPrefill: null as string | null,
  /** Sezione da evidenziare nelle Impostazioni (es. "models"). */
  settingsSection: null as string | null,
  shortcutsOpen: false,
  /** Numero di modali aperte: le scorciatoie globali restano sospese. */
  modalDepth: 0,
  /** Incrementato da Ctrl+Enter: la pagina attiva avvia il lavoro. */
  startRequest: 0,
  /** Dialog di prima configurazione della cartella dati (installazione non scrivibile). */
  dataDirDismissed: false,
  /** Drawer console in-app (log motore), persistito. */
  consoleOpen: false,
});

let toastSeq = 0;

export function toast(kind: ToastKind, message: string, title?: string): number {
  const id = ++toastSeq;
  ui.toasts = [...ui.toasts, { id, kind, message, title }];
  window.setTimeout(() => dismissToast(id), TOAST_TTL);
  return id;
}

export function dismissToast(id: number): void {
  ui.toasts = ui.toasts.filter((toastItem) => toastItem.id !== id);
}

export function setConsoleOpen(open: boolean): void {
  ui.consoleOpen = open;
  try {
    localStorage.setItem(CONSOLE_KEY, open ? "1" : "0");
  } catch {
    /* ambiente non browser */
  }
}

export function toggleConsole(): void {
  setConsoleOpen(!ui.consoleOpen);
}

export function navigate(page: Page): void {
  ui.page = page;
}

export function requestOpenFile(): void {
  if (ui.page !== "protect" && ui.page !== "batch") {
    ui.page = "protect";
  }
  ui.openFileRequest += 1;
}

export function goVerify(path: string): void {
  ui.verifyPrefill = path;
  ui.page = "verify";
}

export function goToModels(): void {
  ui.settingsSection = "models";
  ui.page = "settings";
}

export function setTheme(theme: Theme): void {
  ui.theme = theme;
  try {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* ambiente non browser */
  }
}

export function setLanguage(lang: Lang): void {
  setLang(lang);
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {
    /* ambiente non browser */
  }
}

function isLang(value: string | null): value is Lang {
  return value === "it" || value === "en";
}

function isTheme(value: string | null): value is Theme {
  return value === "dark" || value === "light";
}

/** Inizializza tema e lingua da localStorage e applica gli attributi al documento. */
export function initUi(): void {
  let storedTheme: string | null = null;
  let storedLang: string | null = null;
  let storedConsole: string | null = null;
  try {
    storedTheme = localStorage.getItem(THEME_KEY);
    storedLang = localStorage.getItem(LANG_KEY);
    storedConsole = localStorage.getItem(CONSOLE_KEY);
  } catch {
    /* ignore */
  }
  ui.consoleOpen = storedConsole === "1";
  const theme: Theme = isTheme(storedTheme) ? storedTheme : "dark";
  const lang: Lang = isLang(storedLang) ? storedLang : i18n.lang;
  setTheme(theme);
  setLanguage(lang);
  ui.booted = true;
}
