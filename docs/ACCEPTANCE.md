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
| C | **PARZIALE — Windows PASS** | Installer Windows reali `NL-VO-Shield_1.0.0_windows-x64-setup.exe` (99.566.699 B, sha256 `53281bd1…`) e `.msi` (156.984.167 B), `leak_guard=passed`, `signed=false`, auto-update disabilitato (nessuna chiave), `data_dir_policy=install`. Installazione silenziosa NSIS `/S` → exit 0; app avviata; `data/` creata **dentro l’installazione**, nessun `%APPDATA%`; uninstall `/S` exit 0 con `<install>\data` preservata; MSI admin-extract OK. Motore **frozen** usato con `PATH` ridotto a `System32` (nessun Python di sistema): catena `watermark_dsp + c2pa` su `nicola_clone_op1.wav` → `status=protected`, watermark detected conf=1.0, C2PA signature/dataHash validi (trust = ancora locale), receipt valida, output dentro `<install>/data`. `VERIFICATION.json` in `dist/1.0.0`. | macOS/Linux non eseguibili su questa macchina (niente macOS, WSL senza WebKitGTK/Xcode): coperti dalla CI multi-OS, esito da verificare. Artefatti non firmati (nessun certificato) → avviso SmartScreen e auto-update disabilitato, dichiarato. Nel pacchetto base AudioSeal è opzionale (torch non incluso): watermark DSP nativo disponibile; l’anti-clone resta sperimentale. |
| D | **NOT_RUN / USER SETUP REQUIRED** | Lab privato completo (orchestratore ComfyUI, mapping a ruoli, multi-workflow, report A/B, `plan`/`preflight` con `comfy_reachable` e `missing_workflows`, protocollo `docs/TEST_PROTOCOL_VOICE_CLONING.md`, 6 template). Verifica reale: 127.0.0.1:8188 rifiuta la connessione; `selftest` 16/16 PASS con fixture chiaramente marcate TEST. | ComfyUI spento e workflow/modelli non ancora forniti dal committente: nessun benchmark reale, nessuna percentuale simulata. Pesi ECAPA/Whisper assenti → scorer proxy/INCONCLUSIVE. |

**UI e anteprima audio (verifica separata, non un quinto test):** trovato e corretto un bug reale
nel parser WAV 24-bit stereo (buffer di lettura da 256 KiB non multiplo di 6 byte/frame →
disallineamento dei frame e picchi falsi). Corretto con buffer di carry e coperto da test Rust;
profilo RMS del file reale confrontato con `soundfile`: scostamento massimo **0,10 dB**.
Renderer riscritto a path continuo (picchi min/max specchiati + inviluppo RMS, reticolo a secondi,
playhead, A/B sync) e verificato con catture della sola finestra:
`.local/desktop-shots/wave-start.png`, `wave-mid.png`, `wave-ab.png`.
Matrice tecnologie e decisioni: `docs/TECHNOLOGY_MATRIX.md`.

**Regola mantenuta:** un watermark riuscito non prova difesa anti-clonazione; `protected` è dichiarato solo con tutti gli step richiesti eseguiti e verificati; il laboratorio non scarica e non installa nulla.

---

## Rilascio pubblico 1.0.0 — pubblicato

**Release:** https://github.com/Nicotina4ever/NL-VO-SHIELD/releases/tag/v1.0.0
(CI `Release` su tag `v1.0.0`: build Windows, macOS, Linux e publish = **success**.)

| Artefatto | Dimensione | SHA-256 |
|---|---:|---|
| `NL-VO-Shield_1.0.0_windows-x64-setup.exe` | 41.294.764 B | `b1d1906404e6b8780c840e3505508550549625bae9920227b88806845d25fcfd` |
| `NL-VO-Shield_1.0.0_windows-x64.msi` | 61.228.352 B | `54ee6ad0d08e622815185131ca32c448abe1546e8ebc8189cd25b9be54252201` |
| `NLVOShield_1.0.0_linux-x86_64.deb` | 104.871.706 B | `9ffcbf0c11cb7e06a592ca7a73afed24c1b22b7250c6b7a30094a4fd10608f4a` |
| `NLVOShield_1.0.0_macos-aarch64.dmg` | 59.356.614 B | `3bed78043ecf05e54ac21a81be22a87c0754b53e06acf992cd56851f9c833b5f` |
| `NLVOShield_1.0.0_macos-aarch64.app.tar.gz` | 58.919.690 B | `ed62d86fd6c43e1d723b2021250aff888e6a417ba85573f3948399680e77113c` |

- **Windows:** NSIS + MSI, sidecar nativo con `c2patool`; installazione silenziosa e avvio verificati
  sulla macchina di sviluppo (vedi TEST C). Artefatti **non firmati**: SmartScreen può avvisare.
- **macOS:** bundle `aarch64` (Apple Silicon) `.app`/`.dmg`, non firmato/notarizzato (niente certificati
  Apple disponibili); Gatekeeper richiede l'apertura manuale la prima volta.
- **Linux:** pacchetto `.deb` (`amd64`). **AppImage non incluso nella release CI**: linuxdeploy/FUSE
  non funziona nel runner container; resta generabile localmente su una macchina con FUSE
  (`bash tooling/package.sh --bundles appimage`). Limite dichiarato, non nascosto.
- **Aggiornamenti:** auto-update disabilitato finché non viene configurata una chiave di firma
  (`TAURI_SIGNING_PRIVATE_KEY`); la UI controlla la release GitHub e propone il download manuale.
- `SHA256SUMS` e `BUILD-INFO.json` sono allegati alla release. Il `BUILD-INFO.json` allegato riflette
  l'ultimo job matrix (macOS) perché gli artifact condividono il nome; i dati per piattaforma sono
  comunque nei singoli manifest/sidecar e nei log CI.


