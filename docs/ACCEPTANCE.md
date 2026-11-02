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

| Test | Stato | Evidenza | Limiti reali |
|---|---|---|---|
| A | — | — | — |
| B | — | — | — |
| C | — | — | — |
| D | — | — | — |
