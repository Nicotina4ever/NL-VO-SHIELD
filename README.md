# NL VO Shield - Nicola Lanci Voice Over Shield

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
- macOS Apple Silicon: `.dmg`/`.app.tar.gz`;
- Linux x64: `.deb`.

Gli installer non firmati mostrano l'avviso standard del sistema operativo: le build
pubbliche sono firmate solo quando il maintainer configura le chiavi di firma. Non serve
Python, Node o Rust: il motore Python è incluso nel pacchetto. **Versione corrente: 1.2.6.**

**Primi passi.**
1. Apri **Proteggi**, trascina un file WAV/MP3/FLAC autorizzato e scegli un preset:
   tre livelli — *Leggero/Pubblicazione*, *Bilanciato/Demo*, *Massimo/Studio* — oppure
   **Personalizzato** per selezionare granularmente i singoli motori, con schede info e stato
   reale di ciascuno.
2. I motori non disponibili sono mostrati come tali (con suggerimento di installazione):
   nessun pulsante morto e nessuna protezione dichiarata se il motore non è realmente eseguito.
3. Avvia il job: ottieni un file protetto, una ricevuta firmata e il report delle operazioni.
4. Usa **Verifica** su un file ricevuto per controllare watermark, C2PA e ricevute locali.
5. Controlla **Motori** e **Diagnostica** per sapere cosa è realmente installato.

**Motori e limiti (onesti).**
- **AudioSeal** (watermark neurale) è *opzionale*: richiede il pacchetto AI opzionale e non è
  incluso nella build base. Senza i pesi il motore risulta non disponibile, non simulato.
- **Watermark DSP** (spread-spectrum, senza ML): disponibile nella build base.
- **C2PA / c2patool**: disponibile; c2patool 0.9.12 è incluso nel payload Windows con la sua
  licenza Apache-2.0 (nelle build macOS/Linux la CI lo fornisce se il secret/asset è disponibile).
- **Anti-clone spettrale** è implementato realmente ma ha stato **experimental**: non esiste
  una prova pubblica che garantisca resistenza a ElevenLabs o ad altri modelli proprietari.
- **C2PA** registra una manifest e una firma verificabile, ma **non garantisce l'identità
  legale** di chi ha firmato: trust del certificato e identità sono fatti distinti.
- Un watermark riuscito **non** prova che la difesa anti-clonazione abbia funzionato.
- **SafeSpeech e altri motori di ricerca** non sono dichiarati operativi se i pesi non sono
  disponibili: il registro motori mostra lo stato reale (`available`, `experimental`,
  `unavailable`) con motivazione.

**Modelli, cache e aggiornamenti.**
- **Tutto vive nella cartella di installazione.** La cartella dati di default è
  `<cartella dell'eseguibile>\data` (portable di default): modelli, cache, certificati,
  ricevute e log restano lì. **Niente `%APPDATA%`, niente cache globali** o condivise con
  altri programmi.
- La **release base funziona offline** e **non scarica modelli**: nessun download silenzioso,
  né al primo avvio né durante una protezione. Se la cartella di installazione non è
  scrivibile (es. MSI in `Program Files`), al primo avvio l'app **chiede all'utente** una
  cartella scrivibile: nessun ripiego silenzioso su AppData.
- Da **Impostazioni → Modelli / Cache** puoi vedere percorso, dimensione e stato, cambiare
  cartella, **pulire la cache** (solo file dell'app) e **riscaricare su richiesta** un modello
  con verifica checksum. I file vengono rimossi solo dopo conferma; le registrazioni
  dell'utente non vengono toccate.
- **Disinstallazione:** rimuove programma e runtime Python. La cartella `<install>\data`
  creata dall'app (ricevute, profili, modelli scaricati) **non viene cancellata
  automaticamente**: se vuoi ripartire pulito, rimuovila a mano dopo la disinstallazione.
- **Aggiornamenti:** solo da release GitHub. L'app controlla l'ultima release e propone il
  download manuale; l'aggiornamento automatico è attivo solo quando il maintainer configura
  una chiave di firma, altrimenti resta **disabilitato** e l'app lo dichiara (nessun
  aggiornamento "simulato").


**Componenti opzionali dalla GUI (1.2.6).** Dalla pagina **Componenti** puoi installare
SafeSpeech, Mimic Blocker, Enkidu ed E2E-VGuard con un clic (o **Installa tutto** in sequenza).
L'app scarica i sorgenti ufficiali, crea un ambiente Python/Micromamba dentro `<install>\data`,
installa le dipendenze, scarica i checkpoint (Hugging Face e Google Drive per E2E) e verifica
runner e runtime. Le installazioni sono **job asincroni** con avanzamento, log persistenti
(`data\logs\component-<id>.log`) e stato reale; un componente è disponibile solo se runtime,
checkpoint e runner sono effettivamente presenti. Sono presenti **Ripara/Aggiorna** e
**Disinstalla**, e i quattro motori sono gestibili anche dalla pagina **Motori**.
La stessa azione Installa/Ripara è usata da Motori e Componenti. Per E2E su Windows eSpeak
viene preparato automaticamente; su Linux/macOS richiede ancora eSpeak di sistema.

**Tre modalità di interfaccia.** *Semplice* guida con procedura 1-2-3; *Esperto* espone
selezione motori, ordine pipeline, preset e parametri operativi; *Avanzato* mostra parametri
reali per motore, runtime/device, log e diagnostica. La modalità scelta viene ricordata al
riavvio.

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

**Roadmap / integrazioni future.** Non ancora implementate nella 1.2.6 (nessun pulsante
finto, nessuna promessa attiva):
- **watch folder**: monitoraggio di una cartella per proteggere automaticamente i file
  esportati (es. dal DAW);
- **azione/script Reaper**: applicare la protezione al file esportato direttamente dal
  progetto audio.

La base è pronta per farlo: il core Python espone CLI e RPC locali, quindi queste integrazioni
saranno wrapper sopra gli stessi comandi, senza cambiare il motore né la semantica fail-closed.

---

## English

**What it is.** NL VO Shield is a desktop app that helps protect authorized voice recordings
from cloning and undeclared reuse: watermarking, an experimental anti-clone perturbation, a
locally signed receipt and (when configured) a C2PA provenance manifest. It also verifies
files, measures audio quality, runs batches, stores profiles/presets and reports diagnostics.
It is built for voice actors and studios that want to track their own files — **no voice
cloning feature is included**.

**Install.** Download the installer from the repository *Releases* page: Windows x64
(`.exe` NSIS or `.msi`), macOS Apple Silicon (`.dmg`/`.app.tar.gz`), Linux x64 (`.deb`).
No Python, Node or Rust is needed: the Python engine is bundled. Unsigned builds show the
usual OS warning; public builds are signed only when the maintainer configures signing keys.
**Current version: 1.2.6.**

**First steps.** Open **Protect**, drop an authorized WAV/MP3/FLAC file and choose a preset:
three levels — *Light/Publication*, *Balanced/Demo*, *Maximum/Studio* — or **Custom** for
granular engine selection with info cards and the real state of every engine. Unavailable
engines are shown as such (with an install hint): no dead buttons and no "protected" claim
unless the engine actually ran.

**Engines and honest limits.** AudioSeal watermarking is **optional** (requires the optional AI
package; not in the base build, no simulated weights). **DSP spread-spectrum watermark** is
available in the base build; **C2PA** is available and ships c2patool 0.9.12 in the Windows
payload (Apache-2.0; macOS/Linux CI provisions it when available). The **spectral anti-clone** engine is
real but **experimental**: no public evidence guarantees resistance to ElevenLabs or other
proprietary models. **C2PA** proves integrity/signature, **not legal identity**. A successful
watermark does not prove anti-clone protection worked. Research engines without available
weights are reported `unavailable`, never faked.

**Models, cache and updates.** **Everything lives inside the installation folder.** The
default data directory is `<executable folder>/data` (portable by default): models, cache,
certificates, receipts and logs stay there — **no `%APPDATA%`, no global caches**. The **base
release works offline** and never downloads models silently. If the installation folder is not
writable (e.g. MSI in `Program Files`), on first start the app **asks the user** for a writable
folder: no silent fallback to AppData. In **Settings → Models / Cache** you can inspect
paths/sizes/state, change folders, clean the app cache and re-download on request
(checksum-verified, confirmation required). **Uninstall** removes the program and the Python
runtime; the `<install>/data` folder created by the app (receipts, profiles, downloaded models)
is **not deleted automatically** — remove it manually if you want a clean state. Updates come
from GitHub releases: the app checks the latest release and offers a manual download; automatic
update is enabled only when the maintainer configures a signing key, otherwise it is
**disabled** and the app says so.


**Optional engines from the GUI (1.2.6).** The **Components** page installs SafeSpeech,
Mimic Blocker, Enkidu and E2E-VGuard with one click (or **Install all**, sequentially).
The app downloads official sources, creates a Python/Micromamba environment under
`<install>\data`, installs dependencies, downloads checkpoints (Hugging Face; Google Drive
for E2E) and verifies runtime, runner and files. Installs run as **asynchronous jobs** with
progress, persistent logs (`data\logs\component-<id>.log`) and real status; an engine is
available only when runtime, checkpoints and runner are actually present. **Repair/Update**
and **Uninstall** are available, and the same actions are exposed on the **Engines** page.
The same Install/Repair action is shared by Engines and Components. On Windows eSpeak is
prepared automatically for E2E; Linux/macOS still require system eSpeak.

**Three UI modes.** *Simple* gives a 1-2-3 guided flow; *Expert* exposes engine selection,
pipeline order, presets and operational parameters; *Advanced* shows real per-engine
parameters, runtime/device, logs and diagnostics. The selected mode is restored on restart.

**Privacy.** No telemetry. Recordings never leave the machine and are not sent to ElevenLabs or
any other service. Receipts, profiles and C2PA keys stay in the local app data directory. The
network is used only for explicitly requested optional installs and signed updates.

**Architecture.** Svelte 5 UI inside a Tauri 2 (Rust) shell that launches a local Python sidecar
over JSON-RPC stdio; the Python core handles audio I/O, the fail-closed pipeline, engines,
receipts and reports. See `docs/ARCHITECTURE.md` and `docs/PACKAGING.md`.

**Roadmap / future integrations.** Not implemented in 1.2.6 (no fake buttons, no active
promises): a **watch folder** that protects exported files automatically, and a **Reaper
action/script** that protects the exported take directly. The Python core already exposes the
local CLI and RPC, so these will be thin wrappers over the same engine with the same
fail-closed semantics.
**Repository.** https://github.com/Nicotina4ever/NL-VO-SHIELD`n`n**Release 1.2.6.** https://github.com/Nicotina4ever/NL-VO-SHIELD/releases/latest

---

## Autore / Author

Nicola Lanci
Voice Over Actor - Doppiatore e Speaker
www.nicolalanci.com
info@nicolalanci.com
© 2026 Nicola Lanci — NL VO Shield
Codice e release: github.com/Nicotina4ever/NL-VO-SHIELD · Nicotina4ever/NL-VO-SHIELD

