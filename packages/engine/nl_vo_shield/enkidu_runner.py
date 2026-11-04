"""Single-WAV Enkidu UFP runner (official core, no model training).

Optimizes the official universal frequential perturbation once, caches it under
a reusable profile, then applies it to the requested waveform.  This is the
paper's one-time UFP optimization, not training a voice model.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--enkidu-dir", required=True)
    parser.add_argument("--ecapa-dir", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--noise-level", type=float, default=0.1)
    parser.add_argument("--mask-ratio", type=float, default=0.3)
    parser.add_argument("--target-snr-db", type=float, default=24.0)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.enkidu_dir).resolve()))
    from core import Enkidu
    from speechbrain.inference import SpeakerRecognition

    device = args.device
    if device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"

    audio, sr = sf.read(args.input, always_2d=True)
    mono = audio.mean(axis=1).astype(np.float32)
    if sr != 16000:
        from scipy.signal import resample_poly

        mono = resample_poly(mono, 16000, sr).astype(np.float32)
        sr = 16000
    waveform = torch.from_numpy(mono).float().view(1, -1)

    model = SpeakerRecognition.from_hparams(
        source=args.ecapa_dir,
        savedir=str(Path(args.profile_dir) / "ecapa_cache"),
        run_opts={"device": device},
        overrides={"pretrained_path": args.ecapa_dir},
    )
    enkidu = Enkidu(
        model=model,
        steps=max(1, int(args.steps)),
        alpha=0.1,
        mask_ratio=float(args.mask_ratio),
        frame_length=30,
        noise_level=float(args.noise_level),
        noise_smooth=True,
        device=device,
    )

    profile_dir = Path(args.profile_dir) / "enkidu" / str(args.profile)
    profile_dir.mkdir(parents=True, exist_ok=True)
    real_path, imag_path = profile_dir / "noise_real.pt", profile_dir / "noise_imag.pt"
    if real_path.is_file() and imag_path.is_file():
        noise_real = torch.load(real_path, map_location=device)
        noise_imag = torch.load(imag_path, map_location=device)
        reused = True
    else:
        noise_real, noise_imag = enkidu([waveform])
        noise_real = noise_real.to(device)
        noise_imag = noise_imag.to(device)
        torch.save(noise_real.cpu(), real_path)
        torch.save(noise_imag.cpu(), imag_path)
        reused = False

    protected = enkidu.add_noise(waveform, noise_real, noise_imag, noise_smooth=True)
    protected = protected.detach().cpu().numpy().reshape(-1).astype(np.float32)
    # Quality guard: scale the UFP so the final SNR does not destroy the take.
    signal_rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
    delta = protected - mono
    noise_rms = float(np.sqrt(np.mean(delta * delta))) if delta.size else 0.0
    if signal_rms > 0 and noise_rms > 0:
        max_noise = signal_rms / (10.0 ** (float(args.target_snr_db) / 20.0))
        if noise_rms > max_noise:
            protected = np.clip(mono + delta * (max_noise / noise_rms), -1.0, 1.0)
            delta = protected - mono
    print(
        f"NLVS_ENKIDU profile={args.profile} reused={reused} target_snr={float(args.target_snr_db):.1f} device={device} "
        f"delta_max={float(np.abs(delta).max()):.8f} "
        f"noise_rms={float(np.sqrt(np.mean(delta * delta))):.8f}"
    )
    sf.write(args.output, protected, 16000, subtype="PCM_24")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
