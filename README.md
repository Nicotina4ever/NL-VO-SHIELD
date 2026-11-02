# NL VO Shield

Protezione anti-clonazione, watermark e provenienza per la voce — desktop, offline-first.

[NL-VO-SHIELD su GitHub](https://github.com/Nicotina4ever/NL-VO-SHIELD)

---

## Italiano

**Cos'è.** NL VO Shield è un'app desktop per proteggere registrazioni vocali autorizzate da
clonazione e riuso non dichiarato: applica watermark, una perturbazione anti-clone
sperimentale, una ricevuta locale firmata e, dove configurato, una manifest di provenienza
C2PA. Include verifica di un file, misure di qualità audio, batch, profili/preset e diagnostica.
È pensata per doppiatori, speaker e studi che vogliono tracciare i propri file, non per
generare voci sintetiche: **nessuna funzione di clonazione è inclusa**.

**Installazione.** Scarica l'installer dalla pagina *Releases* del repository:
- Windows x64: `.exe` (NSIS) o `.msi`;
- macOS (Apple Silicon / Intel): `.dmg`;
- Linux x64: `.deb` o `.AppImage`.

Gli installer non firmati mostrano l'avviso standard del sistema operativo: le build
pubbliche sono firmate solo quando il maintainer configura le chiavi di firma. Non serve
Python, Node o Rust: il motore Python è incluso nel pacchetto.

**Primi passi.**
1. Apri **Proteggi**, trascina un file WAV/MP3/FLAC autorizzato e scegli un preset.
2. Seleziona i motori disponibili (AudioSeal opzionale, anti-clone spettrale sperimentale).
3. Avvia il job: ottieni un file protetto, una ricevuta firmata e il report delle operazioni.
4. Usa **Verifica** su un file ricevuto per controllare watermark, C2PA e ricevute locali.
5. Controlla **Motori** e **Diagnostica** per sapere cosa è realmente installato.

**Motori e limiti (onesti).**
- **AudioSeal** (watermark neurale) è *opzionale*: richiede il pacchetto AI opzionale e non è
  incluso nella build base. Senza i pesi il motore risulta non disponibile, non simulato.
- **Anti-clone spettrale** è implementato realmente ma ha stato **experimental**: non esiste
  una prova pubblica che garantisca resistenza a ElevenLabs o ad altri modelli proprietari.
- **C2PA** registra una manifest e una firma verificabile, ma **non garantisce l'identità
  legale** di chi ha firmato: trust del certificato e identità sono fatti distinti.
- Un watermark riuscito **non** prova che la difesa anti-clonazione abbia funzionato.
- **SafeSpeech e altri motori di ricerca** non sono dichiarati operativi se i pesi non sono
  disponibili: il registro motori mostra lo stato reale (`available`, `experimental`,
  `unavailable`) con motivazione.

**Modelli, cache e aggiornamenti.**
- La **release base funziona offline** e **non scarica modelli**: nessun download silenzioso,
  né al primo avvio né durante una protezione.
- I modelli opzionali scelti esplicitamente dall'utente finiscono in
  `<cartella dati app>/models`:
  Windows `%APPDATA%\com.nicotina4ever.nlvoshield\models`,
  macOS `~/Library/Application Support/com.nicotina4ever.nlvoshield/models`,
  Linux `~/.local/share/com.nicotina4ever.nlvoshield/models`.
  In **modalità portable** restano in `<cartella dell'eseguibile>\NL VO Shield Data\models`,
  attiva solo se quella cartella è scrivibile. Nessuna cache globale o condivisa con altri programmi.
- Da **Impostazioni → Modelli / Cache** puoi vedere percorso, dimensione e stato, cambiare
  cartella, **pulire la cache** (solo file dell'app) e **riscaricare su richiesta** un modello
  con verifica checksum. I file vengono rimossi solo dopo conferma; le registrazioni
  dell'utente non vengono toccate.
- **Aggiornamenti:** solo da release GitHub. L'app controlla l'ultima release e propone il
  download manuale; l'aggiornamento automatico è attivo solo quando il maintainer configura
  una chiave di firma, altrimenti resta **disabilitato** e l'app lo dichiara (nessun
  aggiornamento "simulato").

**Privacy.** Nessuna telemetria. Le registrazioni non lasciano la macchina e non vengono
inviate a ElevenLabs o ad altri servizi. Le ricevute, i profili e le chiavi C2PA restano nella
cartella dati locale. La rete serve solo per installazioni opzionali richieste esplicitamente
e per gli aggiornamenti firmati.

**Architettura (breve).** UI Svelte 5 dentro una shell Tauri 2 (Rust) che avvia un sidecar
Python locale via JSON-RPC su stdio; il core Python gestisce audio, pipeline fail-closed,
motori, ricevute e report. Dettagli e contratto: `docs/ARCHITECTURE.md`; packaging:
`docs/PACKAGING.md`.

**Sviluppo.** Windows: `powershell -ExecutionPolicy Bypass -File tooling\bootstrap.ps1`
(doppio click sicuro, idempotente). macOS/Linux: `./tooling/bootstrap.sh` (aggiungi
`--allow-download` solo per il runtime Python portatile). Poi `./tooling/run.sh doctor`,
`node tooling/gen-icons.mjs`, `node tooling/leak-guard.mjs`, `tooling/build-engine-sidecar.*`,
`tooling/package.*`.

---

## English

**What it is.** NL VO Shield is a desktop app that helps protect authorized voice recordings
from cloning and undeclared reuse: watermarking, an experimental anti-clone perturbation, a
locally signed receipt and (when configured) a C2PA provenance manifest. It also verifies
files, measures audio quality, runs batches, stores profiles/presets and reports diagnostics.
It is built for voice actors and studios that want to track their own files — **no voice
cloning feature is included**.

**Install.** Download the installer from the repository *Releases* page: Windows x64
(`.exe` NSIS or `.msi`), macOS Apple Silicon/Intel (`.dmg`), Linux x64 (`.deb` or `.AppImage`).
No Python, Node or Rust is needed: the Python engine is bundled. Unsigned builds show the
usual OS warning; public builds are signed only when the maintainer configures signing keys.

**First steps.** Open **Protect**, drop an authorized WAV/MP3/FLAC file, pick a preset and the
available engines, then run the job: you get a protected file, a signed receipt and a report.
Use **Verify** on received files to check watermark, C2PA and local receipts, and **Engines /
Diagnostics** to see what is really installed.

**Engines and honest limits.** AudioSeal watermarking is **optional** (requires the optional AI
package; not in the base build, no simulated weights). The **spectral anti-clone** engine is
real but **experimental**: no public evidence guarantees resistance to ElevenLabs or other
proprietary models. **C2PA** proves integrity/signature, **not legal identity**. A successful
watermark does not prove anti-clone protection worked. Research engines without available
weights are reported `unavailable`, never faked.

**Models, cache and updates.** The **base release works offline** and never downloads models
silently. Explicitly installed optional models live in `<app data dir>/models` (Windows
`%APPDATA%\com.nicotina4ever.nlvoshield\models`, macOS
`~/Library/Application Support/com.nicotina4ever.nlvoshield/models`, Linux
`~/.local/share/com.nicotina4ever.nlvoshield/models`); portable mode uses
`<executable folder>\NL VO Shield Data\models` when writable, never a shared global cache.
In **Settings → Models / Cache** you can inspect paths, sizes and state, change folders, clean
the app cache and re-download on request (checksum-verified, confirmation required). Updates
come from GitHub releases: the app checks the latest release and offers a manual download;
automatic update is enabled only when the maintainer configures a signing key, otherwise it is
**disabled** and the app says so.

**Privacy.** No telemetry. Recordings never leave the machine and are not sent to ElevenLabs or
any other service. Receipts, profiles and C2PA keys stay in the local app data directory. The
network is used only for explicitly requested optional installs and signed updates.

**Architecture.** Svelte 5 UI inside a Tauri 2 (Rust) shell that launches a local Python sidecar
over JSON-RPC stdio; the Python core handles audio I/O, the fail-closed pipeline, engines,
receipts and reports. See `docs/ARCHITECTURE.md` and `docs/PACKAGING.md`.

**Repository.** https://github.com/Nicotina4ever/NL-VO-SHIELD
