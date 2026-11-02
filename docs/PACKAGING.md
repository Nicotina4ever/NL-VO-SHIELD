# NL VO Shield — Packaging e release (nota tecnica)

Documento per maintainer. La fonte di verità della missione è `MISSIONE_MASTER.md`;
il contratto tra moduli è `docs/ARCHITECTURE.md`. Questo file spiega solo *come si costruisce*.

## 1. Regole invarianti

1. **Nessun Python di sistema**: dev usa `..\python_win\python.exe` (Windows) o
   `.local/runtime/<target>` (macOS/Linux). `tooling/bootstrap.*` è l'unico modo supportato
   di preparare l'ambiente. Niente `pip` globale, niente `PATH` modificato.
2. **Nessun download silenzioso**: solo `bootstrap.* --allow-download` scarica un runtime
   CPython (python-build-standalone, SHA-256 fissato). Il prodotto base non scarica modelli.
3. **Nessun percorso assoluto**: gli script scoprono la radice risalendo da `$PSScriptRoot` /
   `${BASH_SOURCE[0]}`; supportano un repo in qualunque cartella.
4. **Repo pubblico pulito**: `tooling/leak-guard.mjs` blocca percorsi di sviluppo, laboratorio
   privato, audio, chiavi, modelli e token. Gira nel packaging e in CI.
5. **Modelli e dati**: la build base non contiene pesi. Regola di prodotto: **tutto dentro
   la cartella di installazione**, niente `%APPDATA%`. La cartella dati di default è
   `<cartella eseguibile>/data` (modelli, cache, certificati, ricevute, log); se non è
   scrivibile (es. MSI in `Program Files`), l'app chiede all'utente una cartella scrivibile
   e non ripiega in silenzio. I modelli opzionali si installano/rimuovono solo su richiesta
   esplicita dalle Impostazioni, con checksum.

## 2. Bootstrap per piattaforma

```powershell
# Windows (idempotente, doppio click sicuro)
powershell -ExecutionPolicy Bypass -File tooling\bootstrap.ps1
# sola verifica, nessuna scrittura
powershell -ExecutionPolicy Bypass -File tooling\bootstrap.ps1 -Check
# fallback DEV se manca ..\python_win (download esplicito e verificato)
powershell -ExecutionPolicy Bypass -File tooling\bootstrap.ps1 -AllowDownload
```

```bash
# macOS / Linux
./tooling/bootstrap.sh                 # richiede un runtime già presente in .local/runtime
./tooling/bootstrap.sh --allow-download # DEV: scarica CPython portatile (checksum verificato)
./tooling/bootstrap.sh --check          # sola verifica
./tooling/bootstrap.sh --venv           # venv opzionale, creato SOLO dal Python portatile
./tooling/run.sh doctor                 # avvia la CLI del motore con il runtime di progetto
./tooling/run.sh --rpc --data-dir .local/tmp/data  # sidecar JSON-RPC (sviluppo)
```

Stato/recovery: `.local/.bootstrap-state.json` registra l'ultima esecuzione; un run
interrotto viene ripreso al successivo (tutti i passi sono idempotenti). Log:
`.local/logs/bootstrap-*.log`.

## 3. Icone applicazione

```bash
cd tooling && npm ci           # sharp + @tauri-apps/cli (solo tooling, non nel prodotto)
node tooling/gen-icons.mjs     # da assets/LogoMio.svg (INALTERATO) a assets/icons/
node tooling/gen-icons.mjs --check
```

Genera `icon.ico`, `icon.icns` (anche su Windows), `icon.png`, `32x32.png`, `128x128.png`,
`128x128@2x.png`, le `Square*Logo.png` per lo Store Windows e `ui-logo.png` per la UI.
Le cartelle iOS/Android emesse dal CLI vengono rimosse: il prodotto è desktop.

## 4. Engine sidecar (payload in `apps/desktop/src-tauri/engine/`)

```powershell
powershell -ExecutionPolicy Bypass -File tooling\build-engine-sidecar.ps1
powershell -ExecutionPolicy Bypass -File tooling\build-engine-sidecar.ps1 -WithAudioSeal  # opzionale, molto più grande
```
```bash
bash tooling/build-engine-sidecar.sh [--with-audioseal]
```

Layout generato (ignorato da Git):

```text
engine/
  runtime/               CPython portatile + dipendenze base + pacchetto nl_vo_shield
                         Windows: runtime\python.exe
                         macOS/Linux: runtime/bin/python3
  frozen/nlvo-engine/    app PyInstaller one-folder (entry: tooling/sidecar_entry.py)
  build-manifest.json    provenienza, hash requirements, esiti health check
  .nlvs-engine-build     marker: solo questa dir può essere pulita e ricostruita
```

- Il sidecar "runtime" si avvia come `runtime/python[.exe] -m nl_vo_shield.rpc --data-dir <dir>`.
- La variante PyInstaller si avvia come `frozen/nlvo-engine/nlvo-engine(.exe) [--rpc]`.
- **torch/audioseal sono esclusi** dal pacchetto base; `-WithAudioSeal` li include
  (PyInstaller esclude solo UI/matplotlib/IPython/tkinter in quel caso).
- Pulizia: il builder rifiuta di cancellare `engine/` se manca il marker (serve `-Force`).
  Nessun altro file del repo viene rimosso.
- Health check reali per ogni entry point: `--version` **e** `health` JSON-RPC su stdio
  (timeout 20 s lato Windows, `timeout 45` lato POSIX). Se nessun entry point risponde,
  la build fallisce con exit 3.
- Dipendenze del runtime: se esiste `packages/engine/pyproject.toml` il builder installa il
  pacchetto engine + dipendenze base con `pip install --target` (senza toccare linterprete);
  altrimenti usa `requirements.txt` o il baseline `tooling/requirements-baseline.txt`.
  Con `--with-audioseal` usa l extra `[ai]` (torch/audioseal) o `requirements-optional.txt`.

Su macOS il payload viene firmato ad-hoc (`codesign --sign -`) per consentire il bundling:
la notarizzazione richiede certificati Apple e non viene dichiarata se non disponibili.

## 5. Packaging installer

```powershell
powershell -ExecutionPolicy Bypass -File tooling\package.ps1                 # nsis,msi
powershell -ExecutionPolicy Bypass -File tooling\package.ps1 -Bundles nsis
powershell -ExecutionPolicy Bypass -File tooling\package.ps1 -SkipSidecar
```
```bash
./tooling/package.sh [--bundles dmg,app] [--skip-sidecar] [--skip-leak-guard]
```

Pipeline: leak-guard → `npm ci`/`npm run build` → sidecar → **verifica provenienza sidecar e
policy dati** → `tauri build --bundles ...` → `dist/<version>/` con nomi stabili,
`SHA256SUMS`, `BUILD-INFO.json` e scansione binaria degli artefatti (`leak-guard --scan`).

Controlli bloccanti del packaging:
- versione attesa **1.0.0** (`-ExpectedVersion` / `--expected-version`: rifiuta mismatch tra
  tauri.conf.json/package.json/Cargo.toml);
- il payload `engine/` deve essere **nativo della piattaforma**: su Windows
  `runtime/python.exe` + `frozen/nlvo-engine/nlvo-engine.exe`, zero `.so`/`.dylib`; su
  macOS/Linux `runtime/bin/python3` + `frozen/nlvo-engine/nlvo-engine`, zero `.exe`; il
  `build-manifest.json` deve dichiarare la piattaforma corretta e non contenere path assoluti
  di build (niente `K:\...`, `/mnt/...`, `python_win`);
- nessun modello/audio nel payload (`*.pth/onnx/safetensors/gguf/wav/...` = 0 file);
- `paths.rs` deve contenere `install_dir.join("data")` e non usare `app_data_dir()` come
  default (policy portable); `LOCALAPPDATA` è ammesso solo come puntatore minimo alle
  impostazioni quando la cartella install non è scrivibile, mai per dati/modelli;
- `BUILD-INFO.json` deve riportare `data_dir_policy: "install"`, `models_bundled: false`,
  publisher/copyright/homepage/contact/repository e la provenienza del sidecar.

Se `tauri build` fallisce per la firma updater, il packaging riprova una volta con
`bundle.createUpdaterArtifacts=false` e registra la build come non firmata.

Output tipici (versione 1.0.0):

```text
dist/1.0.0/
  NL-VO-Shield_1.0.0_windows-x64-setup.exe
  NL-VO-Shield_1.0.0_windows-x64.msi
  NL-VO-Shield_1.0.0_macos-arm64.dmg
  NL-VO-Shield_1.0.0_macos-arm64.app.tar.gz
  NL-VO-Shield_1.0.0_linux-x64.deb
  NL-VO-Shield_1.0.0_linux-x64.AppImage
  SHA256SUMS
  BUILD-INFO.json
```

`BUILD-INFO.json` dichiara `signed`, `auto_update`, `models_bundled: false`, target, hash,
metadati autore e provenienza sidecar. Nessun modello, audio, chiave o dato del lab deve
comparire nel bundle: la scansione blocca.

### 5.1 Installer per-utente e cartella dati

- **NSIS**: `bundle.windows.nsis.installMode = "currentUser"` (installazione nella cartella
  utente, scrivibile) così `<install>/data` funziona senza privilegi.
- **MSI**: per-macchina in `Program Files`, dove la scrittura può essere negata; in quel caso
  l'app rileva `install_writable = false`, espone `needs_data_dir = true` e chiede una cartella
  dati scrivibile. La modalità consigliata per l'uso portable è l'installer **NSIS**.
- **Disinstallazione**: rimuove programma e runtime incluso; la cartella `<install>/data`
  creata dall'app non viene cancellata automaticamente (contiene dati utente). Va rimossa a
  mano se si vuole ripartire puliti.
- **Verifica post-build**: il packaging scansiona gli artefatti per materiale privato e
  verifica che il payload non contenga modelli/pesi; il default dati è ispezionato staticamente
  in `paths.rs` perché deve restare relativo all'installazione.

### 5.2 Metadati bundle

Valori vincolanti, usati anche nel `BUILD-INFO.json` e nella pagina Informazioni:

```text
publisher/author = Nicola Lanci
copyright        = © 2026 Nicola Lanci — NL VO Shield
homepage         = https://www.nicolalanci.com
contact          = info@nicolalanci.com
repository       = https://github.com/Nicotina4ever/NL-VO-SHIELD
```

## 6. Firma e aggiornamenti

- Updater Tauri: richiede `TAURI_SIGNING_PRIVATE_KEY` (+ eventuale
  `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`) nei secret GitHub **e** `bundle.createUpdaterArtifacts`
  + `plugins.updater.pubkey` nella config Tauri. Le chiavi non vanno mai nel repo.
- `.github/workflows/release.yml` allega `latest.json` **solo** se la build è firmata;
  `tooling/make-latest-json.mjs` rifiuta di scriverlo senza `.sig` reali. In assenza della
  chiave pubblica artefatti non firmati e auto-update disabilitato, dichiarato nei log e nel
  `BUILD-INFO.json`.
- Windows SmartScreen / macOS Gatekeeper avviseranno per build non firmate: è una limitazione
  esterna (certificati), non un difetto nascosto.

## 7. CI e release

- `.github/workflows/ci.yml`: guardie (leak-guard + icone), test Python del motore, frontend,
  `cargo check` e bundle installer su `ubuntu-22.04`, `windows-latest`, `macos-14`.
  `setup-python` serve **solo** a PyInstaller/sidecar: il prodotto non usa Python di sistema.
  Ogni bundle viene scansionato per escludere `da_eliminare`, laboratorio privato e runtime locali.
- `.github/workflows/release.yml`: su tag `v*` costruisce la matrice e pubblica la Release con
  `softprops/action-gh-release`; `latest.json` solo se firmato.
- Il laboratorio privato (`_PRIVATE_RESEARCH_LAB`) e la cartella missione non sono nel repo e
  non entrano in CI/artefatti.

## 8. Verifica rapida

```bash
powershell -File tooling/bootstrap.ps1 && powershell -File tooling/bootstrap.ps1   # 2× idempotente
node tooling/leak-guard.mjs --self-test
node tooling/gen-icons.mjs --check
bash -n tooling/bootstrap.sh && bash -n tooling/run.sh
python -c "import yaml,glob; [yaml.safe_load(open(f, encoding='utf-8')) for f in glob.glob('.github/workflows/*.yml')]"
```
