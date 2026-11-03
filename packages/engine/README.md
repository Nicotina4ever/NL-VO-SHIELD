# NL VO Shield — Python engine core (`nl_vo_shield`)

Versione **1.0.0**. Motore locale, offline-first, usato dalla desktop app NL VO Shield
via JSON-RPC su stdio e direttamente da CLI.

Nessun download automatico: la release base **non scarica nulla**. I pesi AI (AudioSeal)
sono opzionali e vengono scaricati solo su richiesta esplicita dell'utente, dentro la
cartella dati dell'applicazione, con verifica SHA-256.

## Interprete Python

Usa **sempre** l'interprete Python di progetto preparato dal bootstrap
(`NLVS_ENGINE_PYTHON`, es. il runtime sotto `.local/runtime/...` o l'interprete fornito
dal bootstrap). **Mai** `python`/`py`/`pip` di sistema, mai un `venv` creato dall'interprete
di sistema.

L'interprete di progetto può essere compilato in modalità *isolated*: la directory corrente
e `PYTHONPATH` potrebbero essere ignorati. Per questo il pacchetto include due launcher
autosufficienti che inseriscono la propria directory in `sys.path`:

```bat
cd packages\engine
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py doctor --json
"%NLVS_ENGINE_PYTHON%" nlvs_rpc.py --data-dir "<data-dir>"
```

In alternativa (ambiente di sviluppo) `pip install -e .` nel Python di progetto rende
disponibile anche `python -m nl_vo_shield.cli ...` e il comando `nlvs`.

## Dipendenze

```bat
"%NLVS_ENGINE_PYTHON%" -m pip install -r requirements.txt
REM opzionale (torch/audioseal e metriche avanzate):
"%NLVS_ENGINE_PYTHON%" -m pip install -r requirements-optional.txt
REM sviluppo/test:
"%NLVS_ENGINE_PYTHON%" -m pip install -r requirements-dev.txt
```

`requirements.txt` contiene solo le dipendenze base (numpy, scipy, soundfile, cryptography).
Installare `requirements-optional.txt` **non** scarica i pesi dei modelli.

## Comandi principali

```bat
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py doctor --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py engines --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py protect <input.wav> --output-dir <dir> --preset studio_master --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py verify <file_protetto.wav> --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py quality <file.wav> --reference <originale.wav> --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py batch <a.wav> <b.wav> --output-dir <dir> --preset professional_demo --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py watch --input-dir <cartella_export> --output-dir <dir> --preset public_distribution
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py models list --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py models download audioseal-generator --yes
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py models download audioseal-detector --yes
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py cache size --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py receipts list --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py rpc --data-dir <dir>
```

Codici di uscita: `0` successo/`protected`, `2` risultato fail-closed
(partial/failed/indeterminate/not_detected), `1` errore d'uso, `3` doctor con check falliti.

`watch` è un watcher a polling di una cartella (utile per cartelle di export DAW). Non è
una integrazione con Reaper: nessun plugin/estensione viene installato.

## Preset (catene reali, 3 livelli + custom)

I preset sono catene eseguite davvero dal pipeline, non etichette. Esposti da
`presets.list` con `id`, `label`, `level`, `description`, `impact`, `chain`,
`quality_warning`, `watermark_engine`.

| id | livello | catena effettiva |
|---|---|---|
| `public_distribution` | `light` ("Leggero") | watermark (`audioseal` se installato, altrimenti `watermark_dsp`) → `c2pa` sign |
| `professional_demo` | `balanced` ("Bilanciato") | `spectral_cloak` (strength `low`) → watermark → `c2pa` |
| `studio_master` | `strong` ("Massimo") | `spectral_cloak` (strength `medium`) → watermark → `c2pa` (con warning qualità) |
| `base_watermark` | `light` (alias) | `watermark_dsp` → `c2pa` (installazione base senza torch) |
| `custom` | `custom` | catena fornita dalla UI, validata |

`watermark_auto` è un segnaposto risolto a runtime: AudioSeal quando i pesi sono presenti,
altrimenti il watermark DSP. La UI vede sempre la catena risolta.

## Validazione della catena

Il pipeline rifiuta combinazioni rischiose prima di elaborare:

* passi duplicati (`CHAIN_DUPLICATE_STEP`);
* più di una perturbazione anti-clone nella stessa catena (`CHAIN_INCOMPATIBLE_ANTICLONE`);
* engine di misura (`quality`, `fingerprint`) usati come passi di protezione;
* catene vuote.

La catena esplicita della UI viene eseguita così com'è (stessi engine/passi in
`requested_engines` / `executed`); l'unico riordino ammesso è spostare la firma C2PA in
fondo (con warning nel risultato), perché la firma deve coprire l'audio finale.

## Fail-closed (regola non negoziabile)

`status=protected` **solo** se ogni passo `required` è stato eseguito e tutte le verifiche
necessarie sono passate. In ogni altro caso `failed`/`partial`/`indeterminate`: nessun file
viene mai scritto nel path "protected". Per il recupero viene scritto un file distinto
`*_PARTIAL_<jobid>.*` marcato, e il risultato lo dichiara (`output_partial_path`,
`partial_output_marked`). Questo corregge il bug del vecchio launcher che copiava
`*_PROTETTO.wav` anche dopo il fallimento della firma.

## Dati, modelli e cache

Tutto vive nella cartella dati (`--data-dir`, default di sviluppo `<repo>/.local/data`):

```text
<data-dir>/
├── models/audioseal/generator_base.pth   (+ detector_base.pth)
├── cache/                                 (cache torch/HF, solo se serve)
├── c2pa/                                  (identità di firma locale, chiavi 0600)
├── keys/                                  (Ed25519 ricevute + chiave watermark)
├── receipts/                              (ricevute firmate)
├── profiles.json / presets.json / settings.json
```

Prima di importare torch/audioseal vengono forzate `AUDIOSEAL_CACHE_DIR`, `TORCH_HOME`,
`HF_HOME`, `HF_HUB_CACHE`, `XDG_CACHE_HOME` dentro la cartella dati: nessuna scrittura in
cache globali dell'utente. `cache.clear` funziona solo su directory di proprietà del
programma (marker): una cartella scelta dall'utente con file propri non viene mai svuotata.

Download espliciti (RPC `models.download` richiede `confirmed=true`; CLI richiede `--yes`),
con progress e verifica SHA-256; nessun fetch silenzioso.

## Engine e stato (onestà)

| engine | tipo | stato reale |
|---|---|---|
| `audioseal` | watermark | `available` se torch/audioseal e pesi presenti; altrimenti `unavailable` + istruzioni (nessun download automatico); `verified` dopo un'esecuzione reale nella sessione |
| `watermark_dsp` | watermark | `available` (DSSS reale, chiave locale; robustezza codec non validata) |
| `spectral_cloak` | anti_clone | `experimental`: perturbazione STFT reale con distorsione misurata; **efficacia anti-clone non validata** |
| `c2pa` | provenance | `available` se `c2patool` 0.9.x è presente; identità locale generata al primo sign (certificato self-signed: firma valida, trust pubblico separato) |
| `fingerprint` | fingerprint | `available` (profilo spettrale locale per collegare le ricevute) |
| `quality` | quality | `available` (peak/true-peak/LUFS/clipping/SNR/distanza spettrale) |
| `safespeech` | anti_clone | `unavailable` reale: pesi incompleti (manca `D_0.pth`), mai scaricato automaticamente |

Mai etichettato "protetto contro ElevenLabs": il watermark è un aiuto all'attribuzione, la
perturbazione è sperimentale, C2PA non stabilisce un'identità legale automatica.

## RPC (JSON-RPC 2.0, NDJSON su stdio)

Avvio: `nlvs_rpc.py --data-dir <dir>`. Log solo su stderr; una richiesta/risposta per riga
su stdout. Metodi: `health`, `engines.list`, `engines.install_hint`, `job.start`,
`job.cancel`, `job.status`, `job.list`, `verify.run`, `quality.analyze`, `batch.start`,
`doctor.run`, `profiles.list/save/delete`, `presets.list/save/delete`,
`settings.get/set`, `receipts.list/get/verify`, `models.list`, `models.download`
(asincrono, richiede `confirmed=true`), `models.delete`, `models.prune`,
`cache.size`, `cache.clear`, `open_path`, `shutdown`.

Eventi engine→host: `job.progress`, `job.finished`, `job.failed` (per risultati
fail-closed), e per i download `models.progress`, `models.finished`, `models.failed`.
L'`id` restituito da `job.start`/`batch.start`/`models.download` è **esattamente** quello
usato nelle notifiche e in `job.status`.

## Verifica di un file

`verify` combina: detection watermark (AudioSeal e DSP), C2PA (firma + integrità + trust
separati, tamper rilevato), fingerprint e ricevuta locale firmata Ed25519. Esiti:
`verified`, `partial`, `not_detected`, `indeterminate` con spiegazione. La sola somiglianza
di fingerprint non basta: un match senza watermark/hash esatto resta `partial`.

## Test

```bat
cd packages\engine
"%NLVS_ENGINE_PYTHON%" -m pytest -q
```

Copertura: I/O audio e atomicità, qualità, watermark DSP (embed/detect/PCM16), spectral
cloak (seed/distorzione/silenzio), ricevute Ed25519, C2PA (sign/verify/tamper/trust/percorsi
relativi), AudioSeal (skip onesto se i pesi non sono installati), fail-closed (engine
required che fallisce o manca → nessun output protetto), preset e validazione catene,
CLI (launcher con Python isolato, exit code, watch `--once`), RPC (id coerenti, progress,
finished/failed, batch, models). Include `tests/test_qwen_lab.py` per il laboratorio.

## Laboratorio di prova Qwen3-TTS (opzionale, esplicito)

`nl_vo_shield/qwen_lab.py` implementa un laboratorio **A/B** che misura se una voce
protetta è più difficile da clonare: lo stesso testo viene sintetizzato con Qwen3-TTS
usando come riferimento prima l'audio originale e poi l'audio protetto; i due cloni
vengono confrontati con metriche di qualità e speaker similarity ECAPA (se gli asset
sono disponibili). **Non** fa parte della pipeline di protezione e non tocca il desktop.

Regole:
* runtime, modelli e pacchetti sono installati **solo su richiesta esplicita**; nessun
  download implicito, mai al primo avvio;
* il runtime riusa il venv AI esistente (`<data>/runtime/ai/venv`, quello di AudioSeal).
  Se `pip` trova un conflitto di dipendenze viene creato un venv dedicato
  `<data>/runtime/qwen/venv` e lo stato lo dichiara (`isolated=true` + motivo);
  PyTorch non viene duplicato quando è già presente nel venv condiviso;
* i modelli vanno in `<data>/models/qwen3-tts/<dir>` e `<data>/models/whisper/<size>`;
  un `local_path` già scaricato viene **registrato senza copiare nulla**;
* `ref_text` è obbligatorio salvo `x_vector_only=true` (trascrivi prima con Whisper);
* il seed (Python/NumPy/Torch) è impostato prima di **ogni** generazione;
* senza asset ECAPA la similarità è `available=false` con motivo reale: nessun numero
  inventato. In sviluppo gli asset possono essere indicati con `NLVS_ECAPA_DIR`;
* GPU solo se il runtime PyTorch la supporta davvero; altrimenti `device=cpu`.

CLI:

```bat
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab status --json
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab install-engine --device cpu
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab install-model qwen3-tts-0.6b
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab install-model qwen3-tts-tokenizer
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab install-whisper tiny
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab transcribe <riferimento.wav> --model tiny --language it
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab run --original <orig.wav> --protected <prot.wav> ^
    --text "Testo nuovo." --ref-text "<trascrizione del riferimento>" --model qwen3-tts-0.6b --seed 0
"%NLVS_ENGINE_PYTHON%" nlvs_engine.py lab verify --deep
```

RPC (`nlvs_rpc.py`), stessi eventi asincroni con prefisso `lab.`
(`lab.progress`, `lab.finished`, `lab.failed`):

| Metodo | Parametri | Note |
|---|---|---|
| `lab.status` | `{}` | runtime, modelli Qwen/Whisper, device reale, ECAPA, percorsi |
| `lab.install_engine` | `{device:"cpu"\|"cuda"\|"auto"}` | pip nel venv AI (async job) |
| `lab.uninstall_engine` | `{remove_models?:bool}` | rimuove solo ciò che è del laboratorio |
| `lab.install_model` | `{id, source?, local_path?}` | download async; `local_path` = registrazione sincrona, nessuna copia |
| `lab.uninstall_model` | `{id}` | spazio recuperato; `local_path` esterni mai cancellati |
| `lab.install_whisper` | `{size, local_path?}` | pip faster-whisper + download esplicito (async) |
| `lab.uninstall_whisper` | `{size}` | rimuove il modello della taglia |
| `lab.transcribe` | `{audio, model, language?}` | ritorna `{transcription:{text,...}}` |
| `lab.run` | `{original, protected, model_id?, text, ref_text?, language?, seed?, device?, x_vector_only?, output_dir?}` | async job; risultato con WAV cloni, qualità, similarità |
| `lab.verify` | `{deep?}` | import + file attesi + device (+ load reale con `deep`) |

Esito di `lab.run`: `{status:"completed", model, text, ref_text, language, seed, device,
reference:{original,protected}, outputs:{original_clone:{path,sha256,...},
protected_clone:{...}}, quality:{...}, speaker_similarity:{available, original_clone_vs_reference,
protected_clone_vs_reference, delta_protected_minus_original, ...}, timings, warnings}`.
I confronti sono di laboratorio su voce autorizzata: non provano il comportamento di
ElevenLabs né garantiscono una protezione.
