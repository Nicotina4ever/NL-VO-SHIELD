/** Dialoghi nativi (file/cartella/salvataggio) tramite il plugin Tauri dialog. */
import { open, save } from "@tauri-apps/plugin-dialog";
import { isTauri } from "./ipc";

export const AUDIO_EXTENSIONS = ["wav", "mp3", "flac", "ogg", "m4a", "aac", "opus"];

export async function pickAudioFile(options: { multiple?: boolean; title?: string } = {}): Promise<
  string[] | null
> {
  if (!isTauri()) return null;
  try {
    const result = await open({
      title: options.title,
      multiple: options.multiple ?? false,
      directory: false,
      filters: [
        { name: "Audio", extensions: AUDIO_EXTENSIONS },
        { name: "Tutti i file", extensions: ["*"] },
      ],
    });
    if (result == null) return null;
    return Array.isArray(result) ? result : [result];
  } catch {
    return null;
  }
}

export async function pickDirectory(title?: string): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    const result = await open({ title, directory: true, multiple: false });
    if (result == null) return null;
    return Array.isArray(result) ? (result[0] ?? null) : result;
  } catch {
    return null;
  }
}

export async function pickSavePath(options: {
  title?: string;
  defaultPath?: string;
  filters?: { name: string; extensions: string[] }[];
}): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    return await save({
      title: options.title,
      defaultPath: options.defaultPath,
      filters: options.filters,
    });
  } catch {
    return null;
  }
}
