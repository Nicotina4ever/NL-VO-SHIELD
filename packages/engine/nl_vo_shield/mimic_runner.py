"""Single-WAV runner for Mimic Blocker's pretrained generator.

Executed by the isolated torch runtime. It uses the official Generator and
generator_wavlm.pth checkpoint; it never trains anything. The official inference
adds 0.7 * generator(x); we expose a bounded mix for intensity and attenuate to
avoid clipping instead of hard-clamping the voice.
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
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--mix", type=float, default=0.7)
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--target-snr-db", type=float, default=20.0)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.model_dir)))
    from model import Generator  # official model.py

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    net = Generator().to(device)
    state = torch.load(args.checkpoint, map_location=device)
    net.load_state_dict(state)
    net.eval()

    audio, sr = sf.read(args.input, always_2d=True)
    out = np.empty_like(audio, dtype=np.float32)
    with torch.no_grad():
        for ch in range(audio.shape[1]):
            x = torch.from_numpy(audio[:, ch]).float().to(device).view(1, 1, -1)
            noise = net(x).view(-1).cpu().numpy()
            signal = audio[:, ch]
            perturbation = float(args.mix) * noise
            # Quality guard: never let the generator destroy the take.  The
            # official generator can output near-saturation on some speakers.
            sig_rms = float(np.sqrt(np.mean(signal * signal))) if signal.size else 0.0
            noi_rms = float(np.sqrt(np.mean(perturbation * perturbation))) if perturbation.size else 0.0
            if sig_rms > 0 and noi_rms > 0:
                max_noise = sig_rms / (10.0 ** (float(args.target_snr_db) / 20.0))
                if noi_rms > max_noise:
                    perturbation *= max_noise / noi_rms
                    noi_rms = max_noise
            mixed = signal + perturbation
            peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
            ceiling = 0.999
            if peak > ceiling:
                mixed *= ceiling / peak
            out[:, ch] = np.clip(mixed, -1.0, 1.0)
    sf.write(args.output, out, sr, subtype="PCM_24")
    delta = out - audio
    print(
        f"NLVS_MIMIC mix={float(args.mix):.3f} target_snr={float(args.target_snr_db):.1f} device={device} "
        f"delta_max={float(np.abs(delta).max()):.8f} "
        f"noise_rms={float(np.sqrt(np.mean(delta * delta))):.8f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
