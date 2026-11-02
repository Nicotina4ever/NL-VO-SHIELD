# NL VO Shield — Architettura e contratto di integrazione

**Missione:** `MISSIONE_MASTER.md` fornito con il progetto (fonte unica di verità).
Questo documento è il **contratto tecnico** tra i moduli: non è una roadmap.

## 1. Struttura del repository pubblico

```text
NL-VO-SHIELD/                      <-- unico root Git pubblico
├── apps/desktop/                  <-- Tauri 2 + Rust + Svelte 5/TS
│   ├── src/                       <-- UI Svelte (IT/EN), pagine, store
│   ├── src-tauri/                 <-- Rust: comandi, job manager, sidecar bridge
│   └── package.json / vite.config.ts
├── packages/engine/               <-- Python core (nl_vo_shield)
│   ├── nl_vo_shield/              <-- audio, pipeline, engines, rpc, cli, receipts...
│   ├── tests/
│   └── requirements*.txt
├── assets/                        <-- LogoMio.svg (copia esatta), icone generate
├── tooling/                       <-- bootstrap, build, package, CI helpers
├── docs/                          <-- ARCHITECTURE.md (questo file), note tecniche
├── .github/workflows/             <-- CI multi-OS
└── .local/                        <-- runtime/venv/cache SVILUPPO (ignorato da Git)
```

Fuori dal repo (mai tracciati): la distribuzione Python di progetto nel dev root, il laboratorio
privato, la cartella di missione, la quarantena e i dati legacy.

## 2. Python: regole vincolanti

- **Interprete di progetto (dev):** distribuzione Python embedded nel dev root, accanto al repo (Python 3.11).
  - Non esiste `venv`/`ensurepip` in questa distribuzione: non creare venv.
  - `pip` è stato riparato con `get-pip.py` incluso; le dipendenze del motore si installano **in questo interprete**.
- **Fallback di sviluppo:** `.local/runtime/<os>-<arch>/` (python-build-standalone) usato solo se la distribuzione Python di progetto non esiste.
- **Mai** `python`/`py`/`pip` dal PATH, **mai** Python di sistema, **mai** `PATH` globale modificato.
- **Nessun percorso assoluto** in codice/config/script: la radice si scopre risalendo dal file/`__file__`
  o da variabile d'ambiente (`NLVS_PROJECT_ROOT`, `NLVS_ENGINE_PYTHON`) passata dalla shell Rust.
- Pacchetti di motori AI pesanti (torch/audioseal) possono esistere nell'ambiente di sviluppo;
  nella release base restano **opzionali e su richiesta esplicita** (nessun download silenzioso).

### Risoluzione del sidecar Python (ordine)

1. `NLVS_ENGINE_PYTHON` (path a un eseguibile Python) — usato in sviluppo e dai test.
2. Bundle Tauri: `<resource_dir>/engine/runtime/python.exe` (Windows) /
   `<resource_dir>/engine/runtime/bin/python3` (macOS/Linux).
3. Dev fallback relativo: la distribuzione Python di progetto rispetto al repo.
4. `.local/runtime/<os>-<arch>/python[.exe]`.
5. Errore esplicito `ENGINE_PYTHON_NOT_FOUND` (nessun fallback al PATH).

## 3. Protocollo Engine (JSON-RPC 2.0 su stdio, NDJSON)

Trasporto: una richiesta/risposta JSON per riga su **stdout**; log solo su **stderr**.
Il Python di progetto è una distribuzione **isolata** (`sys.flags.isolated=1`): `PYTHONPATH`
e `-m` non sono affidabili. Il pacchetto si avvia con i launcher dedicati:

```bash
"%NLVS_ENGINE_PYTHON%" "<repo>/packages/engine/nlvs_rpc.py" --data-dir "<data-dir>"
"%NLVS_ENGINE_PYTHON%" "<repo>/packages/engine/nlvs_engine.py" doctor --json
```

- `id` numerico o stringa, echo nella risposta.
- Risposta: `{"jsonrpc":"2.0","id":<id>,"result":{...}}` oppure `{"jsonrpc":"2.0","id":<id>,"error":{"code":<int>,"message":"...","data":{...}}}`.
- **Notifiche** engine→host (senza `id`): `{"jsonrpc":"2.0","method":"job.progress","params":{...}}`.
- Timeout host per singola richiesta: default 30 s; `job.start` risponde subito con `job_id` (il lavoro è async).

### 3.1 Metodi

| Metodo | Parametri | Risultato |
|---|---|---|
| `health` | `{}` | `{version, os, arch, python, engines, data_dir}` |
| `engines.list` | `{}` | `{engines:[EngineInfo]}` |
| `engines.install_hint` | `{engine_id}` | `{instructions, requires_download:boolean}` |
| `job.start` | `{input_path, output_dir, chain:[step], options, preset?}` | `{job_id}` |
| `job.cancel` | `{job_id}` | `{cancelled:boolean}` |
| `job.status` | `{job_id}` | `{job_id, state, progress, result?}` |
| `job.list` | `{}` | `{jobs:[...]}` |
| `verify.run` | `{path, receipts_dir?}` | `VerifyResult` |
| `quality.analyze` | `{path, reference_path?}` | `QualityResult` |
| `batch.start` | `{inputs:[path], output_dir, chain, options}` | `{job_id}` |
| `doctor.run` | `{}` | `{checks:[{id,status,detail}], summary}` |
| `profiles.list` / `profiles.save` / `profiles.delete` | `{}` / `{profile}` / `{id}` | `{profiles}` / `{profile}` |
| `presets.list` / `presets.save` | `{}` / `{preset}` | `{presets}` |
| `settings.get` / `settings.set` | `{}` / `{settings}` | `{settings}` |
| `receipts.list` / `receipts.get` | `{}` / `{id}` | `{receipts}` / `{receipt}` |
| `receipts.verify` | `{id}` | `{valid, reason, receipt}` |
| `open_path` | `{path, kind:"file"|"folder"}` | `{opened:boolean}` (host implementa shell; engine può no-op) |
| `shutdown` | `{}` | `{ok:true}` |

### 3.2 Eventi engine→host

```json
{"jsonrpc":"2.0","method":"job.progress","params":{"job_id":"...","step":"watermark","step_index":2,"pct":42,"message":"AudioSeal embed"}}
{"jsonrpc":"2.0","method":"job.finished","params":{"job_id":"...","result":<JobResult>}}
{"jsonrpc":"2.0","method":"job.failed","params":{"job_id":"...","result":<JobResult>}}
```

### 3.3 `EngineInfo` (registro motori)

```json
{"id":"audioseal","name":"AudioSeal","version":"0.3.x","kind":"watermark",
 "capabilities":["embed","detect"],"supported_os":["windows","macos","linux"],
 "status":"available|unavailable|experimental|verified",
 "requires":["torch","weights"],"requires_download":false,
 "reason":null,"path":"...","quality_note":"..."}
```

Tipi: `anti_clone`, `watermark`, `provenance`, `fingerprint`, `quality`.
`status=verified` solo dopo prova reale registrata; `experimental` = implementato ma efficacia non validata.

### 3.4 Step della chain

```json
{"engine":"audioseal","action":"embed","required":true,
 "params":{"payload_hex":"...","sample_rate":16000}}
{"engine":"spectral_cloak","action":"perturb","required":true,"params":{"strength":"medium","seed":1234}}
{"engine":"c2pa","action":"sign","required":true,"params":{"manifest":{...},"cert":"auto"}}
```

Ordine consigliato: anti-clone → watermark → (metriche) → C2PA (ultimo, dopo ogni trasformazione).

### 3.5 `JobResult` (semantica obbligatoria)

```json
{"job_id":"...","status":"protected|partial|failed|indeterminate|cancelled",
 "requested":["anti_clone","watermark","c2pa"],
 "executed":[{"engine":"audioseal","action":"embed","version":"...","params":{}}],
 "verified":[{"check":"watermark.detect","ok":true,"detail":{}}],
 "skipped":[{"engine":"...","reason":"..."}],
 "failed":[{"engine":"...","error":"...","required":true}],
 "quality":{"input":{...},"output":{...}},
 "hashes":{"input_sha256":"...","output_sha256":"..."},
 "output_path":"...","output_partial_path":null,
 "receipt_id":"...","warnings":["..."],"started_at":"...","finished_at":"..."}
```

**Regola fail-closed:** `status=protected` **solo** se ogni step `required` è `executed` e ogni
verifica necessaria è `ok`. Qualsiasi fallimento richiesto ⇒ `failed`/`partial` e **nessun**
file dichiarato protetto. Un output parziale per recupero va in un path distinto e marcato.

### 3.6 `VerifyResult`

```json
{"path":"...","sha256":"...","watermark":{"detected":true,"confidence":0.0,"payload_hex":"...","engine":"audioseal"},
 "c2pa":{"present":true,"valid":true,"signer":"...","trusted":false,"claims":{}},
 "fingerprint":{"matched":true,"receipt_id":"...","similarity":0.93},
 "receipt":{"found":true,"id":"...","valid":true},
 "outcome":"verified|partial|not_detected|indeterminate","details":{},"warnings":[]}
```

## 4. Host Rust ↔ UI

- Il bridge Rust apre il sidecar una volta, mantiene mappa `id→oneshot`, inoltra eventi come
  `app.emit("engine://progress", payload)` / `"engine://finished"` / `"engine://failed"`.
- Comandi Tauri tipizzati (almeno): `engine_health`, `list_engines`, `start_job`, `cancel_job`,
  `job_status`, `verify_file`, `analyze_quality`, `start_batch`, `run_doctor`,
  `list_profiles`, `save_profile`, `delete_profile`, `list_receipts`, `verify_receipt`,
  `get_settings`, `set_settings`, `open_path`, `export_report`.
- Nessun percorso hardcoded: `data_dir` = Tauri app data; input/output scelti dall'utente.
- Errori sempre strutturati `{code,message,detail}`; mai panic.

## 5. Motori: stato atteso e regola d'onestà

| id | tipo | implementazione | stato release |
|---|---|---|---|
| `audioseal` | watermark | adapter reale su `audioseal` (torch) | verificato se eseguito; altrimenti unavailable con reason |
| `spectral_cloak` | anti_clone | perturbazione STFT psicoacusticamente vincolata (DSP, reale) | experimental (efficacia anti-clone non provata) |
| `c2pa` | provenance | `c2patool` + certificato utente/test | verified se sign+verify reali |
| `fingerprint` | fingerprint | impronta spettrale locale + matching | available |
| `quality` | quality | peak/true-peak/LUFS/clipping/SNR/spostamento spettrale | available |

Mai etichettare "protetto contro ElevenLabs" senza prova. Mai dichiarare un engine attivo se non esegue.
SafeSpeech: pesi incompleti ⇒ `unavailable` con reason esplicita (BLOCKER documentato, nessuna simulazione).

## 6. Dati e sicurezza

- Dati utente (ricevute, profili, preset, chiavi, output) in `<install>/data/` **dentro la cartella
  dell'installazione** (default portable) oppure in una cartella scelta esplicitamente dall'utente;
  mai `%APPDATA%`, `~/.cache` o altre directory globali come default. Se la cartella non è scrivibile
  l'app chiede una destinazione: nessun fallback silenzioso.
- Chiavi C2PA: generate/importate dall'utente in data dir; **mai** nel repository.
- Scrittura atomica: temp nella dir del job + `os.replace`; cleanup solo dei propri temp.
- Nessun download automatico: engine mancante ⇒ errore/istruzioni, non fetch silenzioso.

## 6.1 Modelli, cache e aggiornamenti (requisito committente)

- **Tutto nella cartella del progetto/installazione:** pesi e cache vanno in `<data_dir>/models` e
  `<data_dir>/cache`; la `<data_dir>` è `<install>/data` (default portable, dentro l'installazione)
  o una cartella scelta dall'utente. Mai cache globali (`~/.cache`, `%LOCALAPPDATA%` di
  altri tool, `HF_HOME`/`TORCH_HOME` di sistema): l'engine forza `TORCH_HOME`, `HF_HOME`,
  `HF_HUB_CACHE`, `XDG_CACHE_HOME` prima di importare torch/audioseal/huggingface_hub.
- **Nessun download automatico:** `models.download` parte solo su azione esplicita dell'utente,
  con progress, checksum e destinazione nella cartella del progetto. `models.delete`/`models.prune`
  e `cache.clear` rimuovono solo materiali propri dell'app; riscaricabili in seguito.
- **Settings UI:** percorsi models_dir/cache_dir, dimensione, elenco pesi presenti/assenti,
  scarica/elimina/pulisci cache/riscarica. La pagina Motori indica per ogni engine se i pesi ci sono.
- **Aggiornamenti futuri via repo GitHub:** Tauri updater configurato su GitHub Releases
  (`https://github.com/Nicotina4ever/NL-VO-SHIELD`) con firma; se la chiave pubblica non è
  configurata, l'app mostra solo controllo versione/note/link (nessun download silenzioso).
  La release CI firma i bundle solo con `TAURI_SIGNING_PRIVATE_KEY` nei secrets; altrimenti
  artefatti non firmati e auto-update dichiarato disabilitato.

## 6.2 Aggiungere nuove tecnologie di protezione (estensibilità)

Il registro motori è progettato per crescere senza toccare la UI (che legge `engines.list`) né il
protocollo (che resta stabile). Per integrare una nuova tecnologia:

1. **Adapter Python** in `packages/engine/nl_vo_shield/engines/`: sottoclasse di `BaseEngine` con
   `id`, `kind` (`anti_clone|watermark|provenance|fingerprint|quality`), `capabilities`, `version`,
   `availability()`, `info()` e i metodi dell'azione (`perturb`, `embed/detect`, `sign/verify`, ...).
   Deve dichiarare `impact`, `limitations`, `recommended_levels`, `incompatible_with`.
2. **Registrazione** in `engines/__init__.py` + eventuale `ModelSpec` in `models.py` (SHA-256 pinnato,
   download SOLO esplicito nella data-dir, mai cache globali). Se il motore non è distribuibile,
   `status=unavailable` con `reason` reale: nessuna simulazione.
3. **Catena/preset**: aggiungere l'engine alle chain dei preset o lasciarlo selezionabile; il
   validatore rifiuta duplicati, step di misura nella catena e più di una perturbazione anti-clone.
4. **Verifica reale**: esecuzione su audio autorizzato, misure di qualità, controllo del risultato
   (`protected` solo se ogni step required è eseguito e verificato), receipt aggiornata.
5. **Test + documento**: test pytest (fail-closed incluso), aggiornamento del registro nel README.
   La UI mostra automaticamente il nuovo motore con scheda info e toggle; nessun hardcoding.

Candidati già censiti (radar missione §6, **non ancora integrati**): Perth, WavMark, CRAW,
Latent-Mark, Enkidu, CloneShield, VoiceCloak, Silence-the-Mimic, audiowmark. Lo stato onesto di
ognuno resta `available/experimental/unavailable` in base a codice e pesi realmente disponibili.
Matrice completa con audit, licenze e decisioni: `docs/TECHNOLOGY_MATRIX.md`.

## 7. Test di accettazione (solo 4, vedi master §8)

A fail-closed reale; B qualità/robustezza; C installabilità multi-OS; D lab ComfyUI.
Risultati in `docs/ACCEPTANCE.md` (creato dal Lead) con PASS/FAIL/NOT_RUN/BLOCKED e artefatti.

## 8. Ownership scritture (anti-conflitto)

| Area | Owner |
|---|---|
| `packages/engine/**`, `packages/engine/requirements*.txt` | engine-core |
| `apps/desktop/**` (UI + Rust) | desktop-tauri |
| `tooling/**`, `.github/**`, `README.md`, `assets/icons/**` | tooling-packaging |
| laboratorio privato (fuori repo) | private-lab |
| `docs/ARCHITECTURE.md`, `docs/ACCEPTANCE.md`, integrazione, git | Lead |
