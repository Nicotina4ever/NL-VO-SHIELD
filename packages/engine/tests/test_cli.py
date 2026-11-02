"""CLI launchers with the isolated project Python: any cwd, exit codes, fail-closed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]
CLI_LAUNCHER = ENGINE_ROOT / "nlvs_engine.py"
RPC_LAUNCHER = ENGINE_ROOT / "nlvs_rpc.py"


def _run(launcher: Path, args: list[str], cwd: Path, timeout: float = 300.0):
    return subprocess.run(
        [sys.executable, str(launcher), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_version_works_from_unrelated_cwd(tmp_path: Path) -> None:
    completed = _run(CLI_LAUNCHER, ["--version"], tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert "NL VO Shield engine" in completed.stdout


def test_doctor_json_after_subcommand(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    completed = _run(CLI_LAUNCHER, ["doctor", "--json", "--data-dir", str(data_dir)], tmp_path)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["schema"] == "nlvs.doctor/1"
    assert report["paths"]["data_dir"] == str(data_dir.resolve())
    assert "summary" in report


def test_info_and_engines_commands(tmp_path: Path) -> None:
    completed = _run(CLI_LAUNCHER, ["info", "--compact", "--data-dir", str(tmp_path / "d")], tmp_path)
    assert completed.returncode == 0, completed.stderr
    health = json.loads(completed.stdout)
    assert health["version"]
    engines = _run(CLI_LAUNCHER, ["engines", "--compact", "--data-dir", str(tmp_path / "d")], tmp_path)
    assert engines.returncode == 0
    assert json.loads(engines.stdout)["engines"]


def test_rpc_launcher_help(tmp_path: Path) -> None:
    completed = _run(RPC_LAUNCHER, ["--help"], tmp_path)
    assert completed.returncode == 0
    assert "--data-dir" in completed.stdout


def test_protect_and_verify_cli_roundtrip(tmp_path: Path, tone_wav: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "out"
    chain = json.dumps([{"engine": "watermark_dsp", "action": "embed", "required": True}])
    protect = _run(
        CLI_LAUNCHER,
        ["protect", str(tone_wav), "--output-dir", str(output_dir), "--chain", chain,
         "--json", "--no-progress", "--data-dir", str(data_dir)],
        tmp_path,
    )
    assert protect.returncode == 0, protect.stderr
    result = json.loads(protect.stdout)
    assert result["status"] == "protected"
    output = result["output_path"]
    assert Path(output).is_file()

    verify = _run(CLI_LAUNCHER, ["verify", output, "--json", "--data-dir", str(data_dir)], tmp_path)
    assert verify.returncode == 0, verify.stderr
    verification = json.loads(verify.stdout)
    assert verification["outcome"] == "verified"
    assert verification["receipt"]["valid"] is True


def test_fail_closed_cli_exit_code_and_no_protected_file(tmp_path: Path, tone_wav: Path) -> None:
    output_dir = tmp_path / "out"
    chain = json.dumps([{"engine": "safespeech", "action": "perturb", "required": True}])
    completed = _run(
        CLI_LAUNCHER,
        ["protect", str(tone_wav), "--output-dir", str(output_dir), "--chain", chain,
         "--json", "--no-progress", "--data-dir", str(tmp_path / "data")],
        tmp_path,
    )
    assert completed.returncode == 2, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "failed"
    assert result["output_path"] is None
    assert [p for p in output_dir.rglob("*") if p.is_file() and "_protected" in p.name] == []


def test_models_download_requires_explicit_confirmation(tmp_path: Path) -> None:
    completed = _run(
        CLI_LAUNCHER,
        ["models", "download", "audioseal-generator", "--json", "--data-dir", str(tmp_path / "data")],
        tmp_path,
    )
    assert completed.returncode == 1
    error = json.loads(completed.stderr)
    assert error["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_settings_and_presets_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    presets = _run(CLI_LAUNCHER, ["presets", "list", "--json", "--data-dir", str(data_dir)], tmp_path)
    assert presets.returncode == 0
    listed = json.loads(presets.stdout)["presets"]
    assert {"studio_master", "base_watermark", "custom"} <= {item["id"] for item in listed}
    settings = _run(CLI_LAUNCHER, ["settings", "get", "--json", "--data-dir", str(data_dir)], tmp_path)
    assert settings.returncode == 0
    assert "models_dir_resolved" in json.loads(settings.stdout)["settings"]


def test_isolated_python_launcher_imports_package(tmp_path: Path) -> None:
    # The project Python ignores cwd/PYTHONPATH; the launcher must still import.
    code = (
        "import runpy, sys; "
        f"sys.argv=['nlvs_engine.py','--version']; "
        f"runpy.run_path(r'{CLI_LAUNCHER}', run_name='__main__')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "NL VO Shield engine" in completed.stdout
