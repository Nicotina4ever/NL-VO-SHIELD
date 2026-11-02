"""JSON-RPC NDJSON server: health, engines.list, job.start with progress/finished."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]
RPC_LAUNCHER = ENGINE_ROOT / "nlvs_rpc.py"


class RpcClient:
    def __init__(self, data_dir: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(RPC_LAUNCHER), "--data-dir", str(data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.notifications: list[dict] = []
        self._queue: queue.Queue = queue.Queue()
        self._id = 0
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._queue.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _next_message(self, timeout: float) -> dict:
        return self._queue.get(timeout=timeout)

    def request(self, method: str, params: dict | None = None, timeout: float = 180.0) -> dict:
        self._id += 1
        request_id = self._id
        assert self.proc.stdin is not None
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self._next_message(max(0.1, deadline - time.time()))
            if message.get("id") == request_id:
                return message
            self.notifications.append(message)
        raise TimeoutError(f"no response for {method}")

    def wait_notification(self, method: str, timeout: float = 180.0) -> dict:
        for item in list(self.notifications):
            if item.get("method") == method:
                self.notifications.remove(item)
                return item
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self._next_message(max(0.1, deadline - time.time()))
            if message.get("method") == method:
                return message
            self.notifications.append(message)
        raise TimeoutError(f"notification {method} not received")

    def close(self) -> str:
        try:
            self.request("shutdown", timeout=15)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        stderr = self.proc.stderr.read() if self.proc.stderr else ""
        return stderr


@pytest.fixture
def rpc(tmp_path: Path):
    client = RpcClient(tmp_path / "rpc-data")
    yield client
    stderr = client.close()
    assert client.proc.returncode == 0, stderr[-2000:]


def test_health_and_catalog(rpc: RpcClient) -> None:
    health = rpc.request("health")["result"]
    assert health["version"]
    assert health["protocol_version"] == 1
    assert health["python"].startswith("3.")
    assert isinstance(health["engines"], list) and health["engines"]

    engines = rpc.request("engines.list")["result"]["engines"]
    ids = {item["id"] for item in engines}
    assert {"watermark_dsp", "c2pa", "spectral_cloak", "safespeech"} <= ids

    models = rpc.request("models.list")["result"]
    assert models["models"]
    assert models["models_dir"]

    settings = rpc.request("settings.get")["result"]["settings"]
    assert settings["models_dir_resolved"]
    assert settings["cache_dir_resolved"]

    hints = rpc.request("engines.install_hint", {"engine_id": "audioseal"})["result"]
    assert hints["no_automatic_download"] is True


def test_unknown_method_returns_structured_error(rpc: RpcClient) -> None:
    response = rpc.request("does.not.exist")
    assert "error" in response
    assert response["error"]["code"] == -32601


def test_invalid_params_error(rpc: RpcClient) -> None:
    response = rpc.request("job.start", {})
    assert "error" in response
    assert response["error"]["code"] == -32602


def test_job_start_progress_and_finished(rpc: RpcClient, tone_wav: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "rpc-out"
    response = rpc.request(
        "job.start",
        {
            "input_path": str(tone_wav),
            "output_dir": str(output_dir),
            "chain": [{"engine": "watermark_dsp", "action": "embed", "required": True}],
        },
    )
    assert "result" in response, response
    job_id = response["result"]["job_id"]

    finished = rpc.wait_notification("job.finished")
    assert finished["params"]["job_id"] == job_id
    result = finished["params"]["result"]
    assert result["status"] == "protected"
    assert result["output_path"] is not None
    assert result["output_path"].startswith(str(output_dir))

    status = rpc.request("job.status", {"job_id": job_id})["result"]
    assert status["state"] == "finished"
    assert status["result"]["status"] == "protected"

    jobs = rpc.request("job.list")["result"]["jobs"]
    assert any(item["job_id"] == job_id for item in jobs)

    progress_events = [
        item for item in rpc.notifications
        if item.get("method") == "job.progress"
        and item.get("params", {}).get("job_id") == job_id
    ]
    assert progress_events, "no job.progress notification for the started job"
    assert all(item["params"]["job_id"] == job_id for item in progress_events)


def test_job_failure_uses_the_same_job_id(rpc: RpcClient, tmp_path: Path) -> None:
    response = rpc.request(
        "job.start",
        {
            "input_path": str(tone_wav_for(tmp_path)),
            "output_dir": str(tmp_path / "fail-out"),
            "chain": [{"engine": "engine_that_does_not_exist", "action": "embed", "required": True}],
        },
    )
    job_id = response["result"]["job_id"]
    failed = rpc.wait_notification("job.failed")
    assert failed["params"]["job_id"] == job_id
    result = failed["params"]["result"]
    assert result["status"] == "failed"
    assert result["job_id"] == job_id
    status = rpc.request("job.status", {"job_id": job_id})["result"]
    assert status["state"] == "failed"
    assert status["job_id"] == job_id


def test_models_download_failure_uses_the_same_job_id(rpc: RpcClient) -> None:
    response = rpc.request(
        "models.download", {"id": "model-that-does-not-exist", "confirmed": True}
    )
    job_id = response["result"]["job_id"]
    failed = rpc.wait_notification("models.failed")
    assert failed["params"]["job_id"] == job_id
    assert failed["params"]["model_id"] == "model-that-does-not-exist"
    assert failed["params"]["result"]["status"] == "failed"


def tone_wav_for(tmp_path: Path) -> Path:
    from .conftest import write_tone

    return write_tone(tmp_path / "jobfail.wav")


def test_doctor_and_quality_via_rpc(rpc: RpcClient, tone_wav: Path) -> None:
    doctor = rpc.request("doctor.run")["result"]
    assert doctor["schema"] == "nlvs.doctor/1"
    quality = rpc.request("quality.analyze", {"path": str(tone_wav)})["result"]
    assert quality["metrics"]["sample_rate"] == 24000
    verify = rpc.request("verify.run", {"path": str(tone_wav)})["result"]
    assert verify["outcome"] in ("not_detected", "partial", "indeterminate")


def test_batch_via_rpc(rpc: RpcClient, tmp_path: Path) -> None:
    from .conftest import write_tone

    first = write_tone(tmp_path / "b1.wav", frequency=300.0)
    second = write_tone(tmp_path / "b2.wav", frequency=600.0)
    response = rpc.request(
        "batch.start",
        {
            "inputs": [str(first), str(second)],
            "output_dir": str(tmp_path / "batch-out"),
            "chain": [{"engine": "watermark_dsp", "action": "embed", "required": True}],
        },
    )
    job_id = response["result"]["job_id"]
    finished = rpc.wait_notification("job.finished")
    assert finished["params"]["job_id"] == job_id
    batch = finished["params"]["result"]
    assert batch["status"] == "protected"
    assert len(batch["items"]) == 2
