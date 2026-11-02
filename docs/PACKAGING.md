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
5. **Modelli**: la build base non contiene pesi. I modelli opzionali stanno in
   `<app_data>/NL VO Shield/models` (o accanto all'eseguibile in modalità portable) e si
   installano/rimuovono solo su richiesta esplicita dalle Impostazioni, con checksum.

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

Pipeline: leak-guard → `npm ci`/`npm run build` → sidecar → `tauri build --bundles ...` →
`dist/<version>/` con nomi stabili, `SHA256SUMS`, `BUILD-INFO.json` e scansione binaria degli
artefatti (`leak-guard --scan`). Se `tauri build` fallisce per la firma updater, il packaging
riprova una volta con `bundle.createUpdaterArtifacts=false` e registra la build come non firmata.

Output tipici:

```text
dist/0.1.0/
  NL-VO-Shield_0.1.0_windows-x64-setup.exe
  NL-VO-Shield_0.1.0_windows-x64.msi
  NL-VO-Shield_0.1.0_macos-arm64.dmg
  NL-VO-Shield_0.1.0_macos-arm64.app.tar.gz
  NL-VO-Shield_0.1.0_linux-x64.deb
  NL-VO-Shield_0.1.0_linux-x64.AppImage
  SHA256SUMS
  BUILD-INFO.json
```

`BUILD-INFO.json` dichiara `signed`, `auto_update`, `models_bundled: false`, target e hash.
Nessun modello, audio, chiave o dato del lab deve comparire nel bundle: la scansione blocca.

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
