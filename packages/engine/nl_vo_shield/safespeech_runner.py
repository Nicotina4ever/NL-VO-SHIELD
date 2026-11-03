"""Single-WAV adapter around the official SafeSpeech protect.py.

This script is executed by the external SafeSpeech Python runtime (CPU or CUDA
build).  It does not train anything: it prepares the one-file filelist that the
official loader expects, calls the official ``protect.main`` perturbation loop,
then applies the resulting noise to the input and writes a protected WAV.

The official algorithm and checkpoints are used unchanged.  WavLM is not part
of the official protection objective (the reference script merely constructs it
and never uses it); it is stubbed here so an unused 360 MB model is not needed.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


def _resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return samples
    from scipy.signal import resample_poly

    return resample_poly(samples, dst, src).astype(np.float32)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--epsilon", type=int, default=8)
    parser.add_argument("--mode", default="SPEC", choices=["SPEC", "SafeSpeech"])
    parser.add_argument("--official-dir", required=True)
    parser.add_argument("--checkpoint-dir", required=True)
    args = parser.parse_args()

    official = Path(args.official_dir).resolve()
    checkpoints = Path(args.checkpoint_dir).resolve()
    os.chdir(official)
    sys.path.insert(0, str(official))
    sys.path.insert(0, str(official / "bert_vits2"))

    import protect  # official module
    import bert_vits2.utils as ss_utils
    from bert_vits2.text.symbols import symbols

    # Use a minimal valid phone sequence.  The official surrogate still runs its
    # full objective; no model is trained.  The BERT tensor is zero-filled, as
    # the official loader does when .bert.pt is absent.
    phones = [s for s in ("h", "ah", "l", "ow") if s in symbols] or ["h", "ah"]
    phones_text = " ".join(phones)
    tones_text = " ".join("0" for _ in phones)
    word2ph_text = " ".join("1" for _ in phones)

    audio, sr = sf.read(args.input, always_2d=True)
    mono = audio.mean(axis=1).astype(np.float32)
    mono24 = _resample(mono, sr, 24000)

    work = Path(tempfile.mkdtemp(prefix="nlvs_safespeech_"))
    wav24 = work / "single_24k.wav"
    sf.write(wav24, mono24, 24000)
    seq_len = len(phones) * 2 - 1  # add_blank=True
    torch.save(torch.zeros(1024, seq_len), work / "single_24k.bert.pt")
    line = (
        f"{wav24.as_posix()}|0|EN|nlvs|{phones_text}|{tones_text}|{word2ph_text}\n"
    )
    filelist = work / "filelist.txt"
    filelist.write_text(line, encoding="utf-8")

    # The official script reads hps.data.training_files from the dataset config.
    original_loader = ss_utils.get_hparams_from_file

    def patched_loader(path):
        hps = original_loader(path)
        hps.data.training_files = str(filelist)
        return hps

    ss_utils.get_hparams_from_file = patched_loader

    class _UnusedWavLM(torch.nn.Module):
        def __init__(self, *a, **k):
            super().__init__()

    protect.WavLMLoss = _UnusedWavLM  # constructed but unused by perturb()

    model_dir = checkpoints / "base_models"
    model_dir.mkdir(parents=True, exist_ok=True)
    marker = model_dir / ".nlvs_safespeech_marker"
    if not marker.exists():
        marker.write_text("nlvs\n", encoding="utf-8")

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    gpu = 0 if device == "cuda" else -1
    # Official script saves the noise under <checkpoint>/<dataset>/noises.
    noise_path = checkpoints / "LibriTTS" / "noises" / f"BERT_VITS2_{args.mode}_LibriTTS.noise"
    if noise_path.exists():
        noise_path.unlink()

    sys.argv = [
        "protect.py",
        "--dataset", "LibriTTS",
        "--model", "BERT_VITS2",
        "--batch-size", "1",
        "--gpu", str(gpu),
        "--mode", args.mode,
        "--epsilon", str(int(args.epsilon)),
        "--perturbation-epochs", str(int(args.epochs)),
        "--checkpoint-path", str(checkpoints),
    ]
    protect.main()

    noises = torch.load(noise_path, map_location="cpu")
    noise = noises[0].detach().cpu().numpy().reshape(-1).astype(np.float32)
    n = min(len(noise), len(mono24))
    protected24 = np.clip(mono24[:n] + noise[:n], -1.0, 1.0)
    protected = _resample(protected24, 24000, sr)
    sf.write(args.output, protected, sr, subtype="PCM_24")
    delta = protected24 - mono24[:n]
    print(
        f"NLVS_SAFESPEECH noise_max={float(np.abs(noise).max()):.8f} "
        f"noise_rms={float(np.sqrt(np.mean(noise * noise))):.8f} "
        f"delta_max={float(np.abs(delta).max()):.8f} device={device}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
