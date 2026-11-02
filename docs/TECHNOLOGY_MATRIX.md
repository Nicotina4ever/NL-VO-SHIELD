# NL VO Shield — Matrice tecnologie (audit 2 novembre 2026)

Stato reale del codice, non promesse: ogni riga è stata verificata sulle fonti pubbliche
(GitHub API: esistenza, attività, licenza, lingua) e confrontata con ciò che è **realmente
eseguito** in NL VO Shield 1.0.0. "Codice pubblico" ≠ "motore integrato": l'integrazione
richiede adapter, dipendenze, pesi e una prova su audio autorizzato.

Legenda stato: **INTEGRATO** = eseguito e verificato in 1.0.0; **ADAPTER-READY** = codice
disponibile, adapter da completare (pesi/GPU/ambiente); **PAPER/DEMO** = nessun repository
ufficiale verificato; **R&D PRIVATO** = solo laboratorio, mai nel prodotto pubblico.

## 1. Anti-clonazione (audio in ingresso)

| Tecnologia | Fonte verificata | Audit repo | Licenza | Stato in 1.0.0 / decisione |
|---|---|---|---|---|
| **E2E-VGuard** (NeurIPS 2025) | `wxzyd123/E2E-VGuard` | attivo 2025-11-11, Python, ~150 MB | n/d | **ADAPTER-READY**: pipeline end-to-end TTS/LLM, richiede encoder/checkpoint e GPU; non integrabile senza pesi verificati. Primo candidato dopo la 1.0.0. |
| **SafeSpeech / SPEC** (USENIX Sec 2025) | `wxzyd123/SafeSpeech` | attivo 2025-05-24, Python, MIT, ~49 MB | MIT | **ADAPTER-READY / PARZIALE**: codice presente nel dev root; pesi incompleti (`DUR_0` presente, `D_0` mancante) → engine `safespeech` **unavailable** con reason reale, nessuna simulazione. Il vecchio comando `--input/--output` non è il runner ufficiale. |
| **Mimic Blocker** (Interspeech 2025) | `yugwangyeol/Mimic-Blocker` | attivo 2025-05-30, Python, ~124 MB | n/d | **ADAPTER-READY**: difesa su feature WavLM/HuBERT, richiede pesi; candidato per voice conversion. |
| **Enkidu** (ACM MM 2025) | `NoobCodeNameless/Enkidu` | attivo 2025-12-24, Python, piccolo | n/d | **ADAPTER-READY**: perturbazione frequenziale universale; utile come alternativa veloce offline, da confrontare con `spectral_cloak`. |
| **AntiFake** | `WUSTL-CSPL/AntiFake` | ultimo push 2024-05-30, Python, ~126 MB | n/d | **PAPER/BASELINE STORICA**: ambiente datato; recuperabile solo come confronto, non come difesa moderna. |
| **CloneShield** | paper arXiv 2505.19119 | nessun repo ufficiale verificato | — | **PAPER/DEMO**; resta riferimento sperimentale. |
| **VoiceCloak** (AAAI 2026) | demo `voice-cloak.github.io` | nessun repo ufficiale verificato | — | **PAPER/DEMO**; difesa diffusion, codice da verificare. |
| **Silence-the-Mimic** (SLT 2026) | paper arXiv 2610.00662 | nessun repo ufficiale verificato | — | **PAPER/DEMO**; mascheramento psicoacustico, alto interesse ma non riproducibile oggi. |
| **RoCo / RoVo** | saggi `smerge.net` | implementazione riproducibile non confermata | — | **PAPER/DEMO**. |
| **NaVo** (Interspeech 2026) | saggio `smerge.net` | codice non confermato | — | **PAPER/DEMO**: usa ambienti udibili (pioggia/babble); non adatto al preset Studio Master. |
| **Prosody Disruptor / VoiceTect** | IEEE Access 2026 | codice da verificare | — | **PAPER/DEMO**. |
| **Spectral Cloak (NL VO Shield)** | implementazione proprietaria DSP | — | progetto | **INTEGRATO**: perturbazione STFT psicoacusticamente vincolata (strength low/medium/high, seed, distorsione misurata). Status **experimental**: efficacia anti-clone non validata contro clonatori reali. |

## 2. Watermark e tracciabilità

| Tecnologia | Fonte | Audit repo | Licenza | Stato in 1.0.0 / decisione |
|---|---|---|---|---|
| **AudioSeal** (Meta) | `facebookresearch/audioseal` | attivo 2026-11-02, Python, ~61 MB | MIT | **INTEGRATO E VERIFICATO**: embed+detect reali su CPU, payload 16 bit, pesi ufficiali scaricati solo su richiesta esplicita in `<data-dir>/models`, SHA-256 pinnato. `detected/confidence` misurati (TEST A/B). |
| **Watermark DSP (NL VO Shield)** | spread-spectrum proprietario | — | progetto | **INTEGRATO**: embed/detect genuini, z-score, payload 16 bit; fallback senza torch. Nel pacchetto base (senza AudioSeal) è il watermark disponibile. |
| **Perth** (Resemble) | `resemble-ai/Perth` | attivo 2026-09-02, Python, MIT, ~36 MB | MIT | **ADAPTER-READY**: API Python e varianti; confronto qualità/robustezza da eseguire. |
| **SilentCipher** (Sony) | `sony/silentcipher` | attivo 2024-07-22, Python, MIT | MIT | **ADAPTER-READY**: rilevante per 44,1 kHz; pesi/ambiente da verificare. |
| **WavMark** | `wavmark/wavmark` | attivo 2024-01-07, Python, MIT | MIT | **ADAPTER-READY**: baseline multi-bit. |
| **Latent-Mark** (Interspeech 2026) | `yenshan0530/Latent-Mark` | attivo 2026-10-29, Python, MIT | MIT | **ADAPTER-READY**: zero-bit (presenza/assenza, non ID utente); robustezza codec da misurare. |
| **CRAW** (2026) | `DavidC1212/craw` | attivo 2026-09-05, Python | **GPL-3.0** | **ADAPTER-READY / LICENZA**: codec-robust; GPL incompatibile con bundle proprietario → solo plugin esterno opzionale. |
| **TimbreWatermarking** (NDSS 2024) | `TimbreWatermarking/TimbreWatermarking` | attivo 2024-10-23, Python | **GPL-3.0** | **ADAPTER-READY / LICENZA**: come sopra. |
| **DeltaMark / Latent Audio Watermarking** | pagina Munich Music Labs | URL GitHub ufficiale non confermato | — | **PAPER/DEMO**. |
| **XAttnMark** (ICML 2025) | pagina progetto | codice ufficiale non confermato | — | **PAPER/DEMO**. |
| **VoxWatermark** (Interspeech 2026) | `wailywang/VoxWatermark` | attivo 2026-07-21, Python | n/d | **BENCHMARK**: utile per confrontare watermark sotto perturbazioni. |
| **C2PA / c2patool** | `contentauth/c2patool` (Rust, Apache-2.0) | attivo 2026-11-02 | Apache-2.0 | **INTEGRATO E VERIFICATO**: firma WAV, manifest con usage terms/autore, verifica integrità e trust separati; c2patool 0.9.12 incluso nel pacchetto con licenza. |
| **Fingerprint percettivo (NL VO Shield)** | impronta spettrale proprietaria | — | progetto | **INTEGRATO**: confronto locale e collegamento alle ricevute; similarità misurata, non certezza. |

## 3. Laboratorio privato (mai nel prodotto pubblico)

Orchestrazione ComfyUI di workflow forniti dall'autore, **nessun download/installazione**.
Stato: orchestratore completo (mapping a ruoli senza ID hard-coded, `/prompt`, `/ws`, `/history`,
multi-workflow, report A/B, protocollo `TEST_PROTOCOL_VOICE_CLONING.md`, `plan`/`preflight`
con `comfy_reachable`). TEST D reale = `NOT_RUN / USER SETUP REQUIRED` finché ComfyUI e workflow
non vengono forniti.

| Avversario locale | Fonte | Audit | Uso previsto |
|---|---|---|---|
| Fish Audio Drama 3 via ComfyUI | `PedroMarinhoDev/ComfyUI-Drama3`, `fishaudio/fish-speech` | attivi, GPL-3.0 | primo workflow di clonazione consigliato |
| Fish Speech S2-Pro | `fishaudio/fish-speech` | attivo 2026-10-29 | confronto architettura complementare |
| Qwen3-TTS | `QwenLM/Qwen3-TTS` | attivo 2026-03-17, Apache-2.0 | multilingue con italiano |
| CosyVoice | `FunAudioLLM/CosyVoice` | attivo 2026-05-25, Apache-2.0 | zero-shot TTS |
| Chatterbox Multilingual | `resemble-ai/chatterbox` | attivo 2026-07-21, MIT | confronto multilingue |
| MOSS-TTS v1.5 | `OpenMOSS/MOSS-TTS` | attivo 2026-09-06, Apache-2.0 | modelli grandi |
| Higgs Audio v3 | `boson-ai/higgs-audio` + pesi HF | attivo 2026-06-05, Apache-2.0 | usare la v3, non la vecchia v2 |
| De-AntiFake (purificazione) | `cyberrrange/De-AntiFake` | attivo 2025-09-10 | test di sopravvivenza della protezione |
| RVCBench (metriche) | `Nanboy-Ronan/RVCBench` | attivo 2026-10-21, CC0-1.0 | speaker similarity, WER, qualità; scoring locale/CSV |
| OmniSealBench | repo archiviato | — | solo riferimento storico |

## 4. Decisioni e limiti

1. **La 1.0.0 integra solo ciò che esegue davvero**: AudioSeal, watermark DSP, Spectral Cloak
   (experimental), C2PA, fingerprint, qualità. Nessun motore finto nella GUI.
2. I motori con codice ma senza pesi/ambiente verificato si dichiarano `unavailable`/roadmap;
   l'utente vede il motivo reale e nessun download silenzioso.
3. Licenze GPL (CRAW, TimbreWatermarking, ComfyUI-Drama3) escluse dai bundle: eventuali adapter
   restano plugin esterni opzionali, con avviso.
4. **Nessuna percentuale di protezione** viene dichiarata: i test misurano qualità, watermark,
   provenienza e robustezza; l'efficacia contro ElevenLabs richiede un benchmark specifico.
5. Procedura per aggiungere un motore: `docs/ARCHITECTURE.md` §6.2 (adapter, registro, preset,
   verifica, test). La UI legge `engines.list`: nessun hardcoding.
