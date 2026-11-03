"""Qwen3-TTS test laboratory: catalog, local paths, safety, validation, RPC."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from nl_vo_shield.context import AppContext
from nl_vo_shield.qwen_lab import (
    QWEN_MODELS,
    QWEN_WORKER_SOURCE,
    WHISPER_MODELS,
    LabError,
    QwenLab,
)


def _fake_model_dir(base: Path, expected: tuple[str, ...]) -> Path:
    for relative in expected:
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-model-bytes")
    return base


def _tone(path: Path, seconds: float = 0.3) -> Path:
    sample_rate = 16000
    time_axis = np.arange(int(sample_rate * seconds)) / sample_rate
    data = (0.2 * np.sin(2 * np.pi * 220 * time_axis)).astype(np.float32)
    sf.write(str(path), data, sample_rate, subtype="PCM_16")
    return path


@pytest.fixture
def lab(tmp_path: Path) -> QwenLab:
    return QwenLab(tmp_path / "data")


def test_catalog_contains_required_entries() -> None:
    ids = [model.id for model in QWEN_MODELS]
    assert ids == ["qwen3-tts-0.6b", "qwen3-tts-1.7b", "qwen3-tts-tokenizer"]
    repos = {model.id: model.repo_id for model in QWEN_MODELS}
    assert repos["qwen3-tts-0.6b"] == "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    assert repos["qwen3-tts-1.7b"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert repos["qwen3-tts-tokenizer"] == "Qwen/Qwen3-TTS-Tokenizer-12Hz"
    assert [model.dirname for model in WHISPER_MODELS] == [
        "tiny", "base", "small", "medium", "large-v3"
    ]


def test_worker_source_contains_required_api() -> None:
    assert "from qwen_tts import Qwen3TTSModel" in QWEN_WORKER_SOURCE
    assert "from_pretrained" in QWEN_WORKER_SOURCE
    assert "generate_voice_clone" in QWEN_WORKER_SOURCE
    assert "def set_seed" in QWEN_WORKER_SOURCE
    assert "torch.manual_seed" in QWEN_WORKER_SOURCE
    assert "snapshot_download" in QWEN_WORKER_SOURCE
    assert "x_vector_only" in QWEN_WORKER_SOURCE
    # It must never depend on the frozen engine package.
    assert "nl_vo_shield" not in QWEN_WORKER_SOURCE


def test_status_shape_without_runtime(lab: QwenLab) -> None:
    status = lab.status()
    assert status["schema"] == "nlvs.qwen-lab-status/1"
    runtime = status["runtime"]
    assert runtime["installed"] is False
    assert runtime["isolated"] is False
    assert runtime["python"] is None
    assert len(status["qwen"]["models"]) == 3
    assert all(not item["installed"] for item in status["qwen"]["models"])
    assert all(not item["present"] for item in status["qwen"]["models"])
    assert status["whisper"]["supported_sizes"] == ["tiny", "base", "small", "medium", "large-v3"]
    assert status["device"]["cpu"] is True
    assert isinstance(status["device"]["cuda"], bool)
    assert "ecapa" in status and "available" in status["ecapa"]
    assert status["paths"]["worker"].endswith("qwen_worker.py")


def test_local_path_registration_does_not_copy(lab: QwenLab, tmp_path: Path) -> None:
    spec = QWEN_MODELS[0]
    local = _fake_model_dir(tmp_path / "already-downloaded", spec.expected_files)
    result = lab.install_model(spec.id, local_path=str(local))
    assert result["status"] == "registered"
    assert result["source"] == "local_path"
    assert Path(result["path"]) == local.resolve()
    assert Path(result["path"]).is_dir()
    # No copy inside the managed models root.
    managed = lab.qwen_root / spec.dirname
    assert not managed.exists()
    entry = lab.installed_entry(spec.id)
    assert entry is not None and entry["source"] == "local_path"
    # Uninstall removes only the registration, never the external files.
    removed = lab.uninstall_model(spec.id)
    assert removed["source"] == "local_path"
    assert removed["freed_bytes"] == 0
    assert local.is_dir() and (local / "model.safetensors").is_file()
    assert lab.installed_entry(spec.id) is None


def test_local_path_incomplete_is_rejected(lab: QwenLab, tmp_path: Path) -> None:
    spec = QWEN_MODELS[0]
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(LabError) as error:
        lab.install_model(spec.id, local_path=str(incomplete))
    assert error.value.code == "LAB_LOCAL_PATH_INCOMPLETE"
    assert "model.safetensors" in error.value.detail["missing"]


def test_uninstall_refuses_path_outside_models_root(lab: QwenLab, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.wav"
    keep.write_bytes(b"user data")
    lab.qwen_root.mkdir(parents=True, exist_ok=True)
    lab._write_registry(
        lab._qwen_registry_file(),
        {"qwen3-tts-0.6b": {"source": "download", "path": str(outside)}},
    )
    with pytest.raises(LabError) as error:
        lab.uninstall_model("qwen3-tts-0.6b")
    assert error.value.code == "LAB_UNINSTALL_UNSAFE"
    assert keep.is_file()


def test_unknown_model_ids_are_rejected(lab: QwenLab) -> None:
    with pytest.raises(LabError):
        lab.install_model("qwen3-tts-99b")
    with pytest.raises(LabError):
        lab.install_whisper("gigantic")


def test_whisper_local_path_roundtrip(lab: QwenLab, tmp_path: Path) -> None:
    spec = WHISPER_MODELS[1]  # base
    local = _fake_model_dir(tmp_path / "whisper-base", spec.expected_files)
    result = lab.install_whisper("base", local_path=str(local))
    assert result["status"] == "registered"
    assert lab.whisper_entry("base") is not None
    removed = lab.uninstall_whisper("base")
    assert removed["freed_bytes"] == 0
    assert local.is_dir()
    assert lab.whisper_entry("base") is None


def test_run_requires_ref_text_unless_x_vector_only(lab: QwenLab, tmp_path: Path) -> None:
    original = _tone(tmp_path / "original.wav")
    protected = _tone(tmp_path / "protected.wav")
    spec = QWEN_MODELS[0]
    local = _fake_model_dir(tmp_path / "model", spec.expected_files)
    lab.install_model(spec.id, local_path=str(local))
    with pytest.raises(LabError) as error:
        lab.run(
            original=original,
            protected=protected,
            text="Ciao, questo è un test.",
            ref_text=None,
            model_id=spec.id,
            x_vector_only=False,
        )
    assert error.value.code == "LAB_REF_TEXT_REQUIRED"
    # With x_vector_only the ref_text is optional; this run stops later because
    # the optional runtime is not installed in the temporary data dir.
    with pytest.raises(LabError) as error:
        lab.run(
            original=original,
            protected=protected,
            text="Ciao, questo è un test.",
            ref_text=None,
            model_id=spec.id,
            x_vector_only=True,
        )
    assert error.value.code in ("LAB_RUNTIME_MISSING", "LAB_MODEL_PATH_MISSING", "LAB_UNAVAILABLE")


def test_run_validates_inputs(lab: QwenLab, tmp_path: Path) -> None:
    original = _tone(tmp_path / "original.wav")
    with pytest.raises(LabError):
        lab.run(
            original=tmp_path / "missing.wav",
            protected=original,
            text="test",
            ref_text="test",
        )
    with pytest.raises(LabError):
        lab.run(
            original=original,
            protected=original,
            text="   ",
            ref_text="test",
        )
    with pytest.raises(LabError):
        lab.run(
            original=original,
            protected=original,
            text="test",
            ref_text="test",
            device="tpu",
        )


def test_ecapa_status_from_configured_assets(lab: QwenLab, tmp_path: Path, monkeypatch) -> None:
    assets = tmp_path / "ecapa-assets"
    assets.mkdir()
    for name in ("hyperparams.yaml", "config.json", "embedding_model.ckpt"):
        (assets / name).write_bytes(b"x")
    monkeypatch.setenv("NLVS_ECAPA_DIR", str(assets))
    status = lab.ecapa_status()
    assert status["available"] is True
    assert Path(status["path"]) == assets.resolve()
    assert status["model"] == "speechbrain-ecapa"


def test_ecapa_missing_reports_unavailable(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "no-ecapa"
    monkeypatch.setenv("NLVS_ECAPA_DIR", str(missing))
    status = QwenLab(tmp_path / "data").ecapa_status()
    assert status["available"] is False
    assert "ECAPA" in (status["reason"] or "")


def test_worker_is_written_before_worker_call(tmp_path: Path) -> None:
    lab = QwenLab(tmp_path)
    lab._ensure_worker()
    assert lab.worker.is_file()
    assert "generate_voice_clone" in lab.worker.read_text(encoding="utf-8")
    # Idempotent rewrite.
    first = lab.worker.stat().st_mtime_ns
    lab._ensure_worker()
    assert lab.worker.stat().st_mtime_ns >= first


def test_lab_status_via_rpc_and_validation() -> None:
    """RPC surface: lab.status works and lab.run validates ref_text synchronously."""
    import subprocess
    import sys
    import time
    import queue as queue_module
    import threading

    engine_root = Path(__file__).resolve().parents[1]
    launcher = engine_root / "nlvs_rpc.py"
    proc = subprocess.Popen(
        [sys.executable, str(launcher), "--data-dir", str(Path(__file__).resolve().parent / "_rpc_lab_data")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    messages: "queue_module.Queue[dict]" = queue_module.Queue()
    threading.Thread(
        target=lambda: [
            messages.put(json.loads(line)) for line in proc.stdout if line.strip()
        ],
        daemon=True,
    ).start()

    def request(method: str, params: dict | None = None, timeout: float = 120.0) -> dict:
        request_id = int(time.time() * 1000) % 10_000_000
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        ) + "\n")
        proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = messages.get(timeout=max(0.1, deadline - time.time()))
            if message.get("id") == request_id:
                return message
        raise TimeoutError(method)

    try:
        status = request("lab.status")
        assert "result" in status, status
        lab = status["result"]["lab"]
        assert lab["schema"] == "nlvs.qwen-lab-status/1"
        assert lab["qwen"]["default_model"] == "qwen3-tts-0.6b"
        run = request("lab.run", {"original": "a.wav", "protected": "b.wav", "text": "ciao"})
        assert "error" in run
        assert "ref_text" in run["error"]["message"]
        assert request("shutdown")["result"]["ok"] is True
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil_data = engine_root / "tests" / "_rpc_lab_data"
        if shutil_data.exists():
            import shutil

            shutil.rmtree(shutil_data, ignore_errors=True)
