# NL VO Shield — Piano e verbale dei 4 test di accettazione

**Regola:** solo quattro prove end-to-end decisive (master §8). Ogni prova termina con
`PASS` / `FAIL` / `NOT_RUN` / `BLOCKED` e artefatti verificabili. Nessun risultato simulato.

Ambiente di esecuzione: Windows 11 x64, interprete Python di progetto embedded nel dev root (3.11.9),
toolchain Node 24 / Rust 1.99 MSVC / VS2019 BuildTools, WebView2 156, ffmpeg presente.
Audio autorizzato: `<dev root>/input/nicola_clone_op1.wav` (voce reale, 48 kHz stereo, 23,2 s).
Nota verificata: `<dev root>/input/test.wav` è **silenzio digitale** (1 s, campioni tutti 0):
non è adatto a dimostrare una perturbazione spettrale e produce correttamente uno stato
`partial` con output `_PARTIAL_` (comportamento fail-closed).

## TEST A — Prodotto reale / fail-closed

Catena reale: anti-clone (se disponibile) → watermark (AudioSeal reale; fallback DSP solo se
dichiarato) → metriche → C2PA (firma). Poi rilettura dell'output: detect watermark, verifica C2PA,
ricevuta locale firmata. Forzare il fallimento di uno step `required` e verificare che **nessun**
file risulti `protected`/completo (output parziale distinto e marcato).

Criteri PASS: `JobResult.status=protected` solo con tutti gli step eseguiti e verificati;
output leggibile e watermark/firma rilevati; caso forzato = `failed` senza output protetto; receipt valida.

## TEST B — Qualità e robustezza

Originale vs protetto: ascolto A/B (misure oggettive: LUFS, true-peak, clipping, differenza spettrale),
poi MP3 (ffmpeg) + resampling e nuova verifica watermark/tracciabilità. Documentare degrado reale
e limiti (AudioSeal può degradare con MP3: riportare il numero, non nasconderlo).

Criteri PASS: misure prodotte e confrontabili; il file protetto resta decodificabile; esito di
robustezza riportato onestamente (PASS/FAIL parziale documentato).

## TEST C — Installabilità multi-OS

Build reale pacchetto Windows (NSIS/MSI) con sidecar Python incluso; avvio/health/CLI senza
Python globale sul PATH; verifica che i bundle non contengano lab, audio, chiavi, modelli,
runtime estranei, quarantena. macOS/Linux: build su runner GitHub Actions (matrice CI) e
artefatti caricati; se non eseguibili localmente, stato = PASS CI / limitazione firma documentata.

Criteri PASS: artefatto Windows installabile e funzionante senza Python di sistema;
CI mac/linux produce artefatti o errore reale documentato; nessun file privato nei pacchetti.

## TEST D — Banco privato realmente controllabile

Research Lab (cartella privata fuori Git) su ComfyUI locale esistente: preflight reale,
import workflow scelto, job originale vs protetto, raccolta output, report A/B. Senza ComfyUI
raggiungibile: `NOT_RUN / USER SETUP REQUIRED`, mai percentuali inventate. Se disponibile un
secondo workflow, ripetere.

Criteri PASS: preflight reale documentato; report generato con dati reali **oppure** NOT_RUN onesto.

---

## Verbale (compilato dal Lead a fine missione)

**Stato al 2 novembre 2026 — build 1.0.0, catena reale AudioSeal + C2PA + perturbazione DSP.**

| Test | Stato | Evidenza | Limiti reali |
|---|---|---|---|
| A | **PASS** | Catena `spectral_cloak → audioseal → c2pa` su `input/nicola_clone_op1.wav`: `status=protected`, output `nicola_clone_op1_protected.wav` (sha256 `26850a00532f8db0…`), AudioSeal detected=true conf=1.0, C2PA valid=true (trust locale, non trust pubblico), receipt Ed25519 valida `0a8a1fc7-…`; caso forzato con engine required inesistente → `status=failed`, nessun file “protected”. Evidence `.local/acceptance/20261102-172530/`. | C2PA usa un’ancora locale autogenerata (non un certificato pubblico); AudioSeal è opzionale nel pacchetto base (torch non incluso); l’anti-clone resta sperimentale e non validato contro modelli di clonazione. |
| B | **PASS** | 48 kHz PCM24 preservati; LUFS −18,529 → −18,038; true-peak −2,99 → −2,84 dBTP; 0 campioni in clipping; SNR 22,94 dB; correlazione 0,99898; distanza spettrale 2,48 dB; dopo MP3 192 kbps e resample 44,1 kHz il watermark AudioSeal resta rilevato (conf 1,0). Evidence `.local/acceptance/20261102-172332/`. | Misure oggettive, non un test di ascolto formale; la robustezza MP3 è un’osservazione misurata, non una garanzia; QoL con codec neurali non testata. |
| C | **PARZIALE → in chiusura** | Installer Windows reali `NL-VO-Shield_1.0.0_windows-x64-setup.exe` (93.989.374 B) e `.msi` (149.275.003 B), `leak_guard=passed`, `signed=false`, auto-update disabilitato (nessuna chiave). Installazione silenziosa riuscita in cartella temporanea; app avviata; dati creati **dentro l’installazione** (`data/{cache,certs,logs,models,output,profiles,receipts,webview2}`), nessuna cartella `%APPDATA%`; sidecar Windows `nlvo-engine.exe` 1.0.0 avviato. | Build macOS/Linux da verificare su runner GitHub Actions (in corso); firma/notarizzazione assente (nessun certificato); risolto in rebuild il bundling di `c2patool` (risultava assente nell’installazione). |
| D | **NOT_RUN / USER SETUP REQUIRED** | Lab privato completo (orchestratore ComfyUI, mapping a ruoli, multi-workflow, report A/B, `plan`/`preflight` con `comfy_reachable` e `missing_workflows`, protocollo `docs/TEST_PROTOCOL_VOICE_CLONING.md`, 6 template). Verifica reale: 127.0.0.1:8188 rifiuta la connessione; `selftest` 16/16 PASS con fixture chiaramente marcate TEST. | ComfyUI spento e workflow/modelli non ancora forniti dal committente: nessun benchmark reale, nessuna percentuale simulata. Pesi ECAPA/Whisper assenti → scorer proxy/INCONCLUSIVE. |

**Regola mantenuta:** un watermark riuscito non prova difesa anti-clonazione; `protected` è dichiarato solo con tutti gli step richiesti eseguiti e verificati; il laboratorio non scarica e non installa nulla.

