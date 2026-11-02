# NL VO Shield — Architettura e contratto di integrazione

**Missione:** `K:\AI\VoiceShield_2026\NL-VO-SHIELD-MISSIONE-COMPLETA\MISSIONE_MASTER.md` (fonte unica di verità).
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

Fuori dal repo (mai tracciati): `K:\AI\VoiceShield_2026\python_win` (Python di progetto),
`_PRIVATE_RESEARCH_LAB`, `NL-VO-SHIELD-MISSIONE-COMPLETA`, `da_eliminare`, dati legacy.

## 2. Python: regole vincolanti

- **Interprete di progetto (dev):** `..\python_win\python.exe` rispetto al repo (Python 3.11.9, embedded).
  - Non esiste `venv`/`ensurepip` in questa distribuzione: non creare venv.
  - `pip` è stato riparato con `get-pip.py` incluso; le dipendenze del motore si installano **in questo interprete**.
- **Fallback di sviluppo:** `.local/runtime/<os>-<arch>/` (python-build-standalone) usato solo se `python_win` non esiste.
- **Mai** `python`/`py`/`pip` dal PATH, **mai** Python di sistema, **mai** `PATH` globale modificato.
- **Nessun percorso assoluto** in codice/config/script: la radice si scopre risalendo dal file/`__file__`
  o da variabile d'ambiente (`NLVS_PROJECT_ROOT`, `NLVS_ENGINE_PYTHON`) passata dalla shell Rust.
- Pacchetti di motori AI pesanti (torch/audioseal) possono esistere nell'ambiente di sviluppo;
  nella release base restano **opzionali e su richiesta esplicita** (nessun download silenzioso).

### Risoluzione del sidecar Python (ordine)

1. `NLVS_ENGINE_PYTHON` (path a un eseguibile Python) — usato in sviluppo e dai test.
2. Bundle Tauri: `<resource_dir>/engine/runtime/python.exe` (Windows) /
   `<resource_dir>/engine/runtime/bin/python3` (macOS/Linux).
3. Dev fallback relativo: `../python_win/python.exe` rispetto al repo.
4. `.local/runtime/<os>-<arch>/python[.exe]`.
5. Errore esplicito `ENGINE_PYTHON_NOT_FOUND` (nessun fallback al PATH).

## 3. Protocollo Engine (JSON-RPC 2.0 su stdio, NDJSON)

Trasporto: una richiesta/risposta JSON per riga su **stdout**; log solo su **stderr**.
Il pacchetto Python si avvia con:

```bash
"%NLVS_ENGINE_PYTHON%" -m nl_vo_shield.rpc --data-dir "<data-dir>"
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

- Dati utente (ricevute, profili, preset, chiavi) in `<app_data>/NL VO Shield/` (Tauri) o
  `--data-dir` passato dal Rust; in portable mode accanto all'eseguibile solo se scrivibile.
- Chiavi C2PA: generate/importate dall'utente in data dir; **mai** nel repository.
- Scrittura atomica: temp nella dir del job + `os.replace`; cleanup solo dei propri temp.
- Nessun download automatico: engine mancante ⇒ errore/istruzioni, non fetch silenzioso.

## 7. Test di accettazione (solo 4, vedi master §8)

A fail-closed reale; B qualità/robustezza; C installabilità multi-OS; D lab ComfyUI.
Risultati in `docs/ACCEPTANCE.md` (creato dal Lead) con PASS/FAIL/NOT_RUN/BLOCKED e artefatti.

## 8. Ownership scritture (anti-conflitto)

| Area | Owner |
|---|---|
| `packages/engine/**`, `packages/engine/requirements*.txt` | engine-core |
| `apps/desktop/**` (UI + Rust) | desktop-tauri |
| `tooling/**`, `.github/**`, `README.md`, `assets/icons/**` | tooling-packaging |
| `_PRIVATE_RESEARCH_LAB/**` (fuori repo) | private-lab |
| `docs/ARCHITECTURE.md`, `docs/ACCEPTANCE.md`, integrazione, git | Lead |
