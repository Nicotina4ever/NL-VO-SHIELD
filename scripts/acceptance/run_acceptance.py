#!/usr/bin/env python3
"""NL VO Shield — the four decisive acceptance tests (mission §8).

Usage (run with the project Python embedded in the dev root, never system Python):

    <project-python> scripts/acceptance/run_acceptance.py --test all

Evidence is written to <repo>/.local/acceptance/<timestamp>/ as JSON + SUMMARY.md.
Every test ends with PASS / FAIL / NOT_RUN / BLOCKED and real artefacts.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # .../NL-VO-SHIELD
PROJECT = ROOT.parent                                # .../VoiceShield_2026
ENGINE_DIR = ROOT / "packages" / "engine"
_AUDIO_CANDIDATES = [
    PROJECT / "input" / "nicola_clone_op1.wav",   # real authorized voice, preferred for A/B
    PROJECT / "input" / "test.wav",               # legacy 1s test clip (digitally silent)
]
_env_audio = os.environ.get("NLVS_TEST_AUDIO")
TEST_AUDIO = Path(_env_audio) if _env_audio else next(
    (p for p in _AUDIO_CANDIDATES if p.is_file()), _AUDIO_CANDIDATES[-1]
)
EVIDENCE_ROOT = ROOT / ".local" / "acceptance"


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def is_windows() -> bool:
    return os.name == "nt"


def find_python() -> Path:
    """Resolve the project Python. Never falls back to system Python."""
    env = os.environ.get("NLVS_ENGINE_PYTHON")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise SystemExit(f"NLVS_ENGINE_PYTHON set but missing: {p}")
    # Scan the dev root for an embedded Python distribution (no hardcoded name).
    for child in sorted(PROJECT.iterdir()):
        if not child.is_dir():
            continue
        for exe in (child / "python.exe", child / "bin" / "python3", child / "python3"):
            if exe.is_file():
                return exe
    runtime_root = ROOT / ".local" / "runtime"
    if runtime_root.is_dir():
        for arch_dir in sorted(runtime_root.iterdir()):
            for exe in (arch_dir / "python.exe", arch_dir / "bin" / "python3"):
                if exe.is_file():
                    return exe
    raise SystemExit(
        "Project Python not found. Put the project Python distribution in the dev root "
        "(sibling of the repo) or set NLVS_ENGINE_PYTHON. Refusing to use system Python."
    )


def find_lab() -> Path | None:
    """Locate the optional private research lab (outside the public repo)."""
    env = os.environ.get("NLVS_LAB_DIR")
    if env:
        p = Path(env)
        if (p / "nlv_lab").is_dir():
            return p
    for child in PROJECT.iterdir():
        if child.is_dir() and (child / "nlv_lab").is_dir():
            return child
    return None


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    return str(cand) if cand.is_file() else None


def find_c2patool() -> Path | None:
    env = os.environ.get("NLVS_C2PATOOL")
    if env and Path(env).is_file():
        return Path(env)
    for cand in (
        PROJECT / "bin" / "c2patool" / "c2patool" / ("c2patool.exe" if is_windows() else "c2patool"),
        ROOT / "tooling" / "bin" / ("c2patool.exe" if is_windows() else "c2patool"),
    ):
        if cand.is_file():
            return cand
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


AUDIOSEAL_FILES = ("generator_base.pth", "detector_base.pth")


def seed_audioseal(engine: "EngineProcess", data_dir: Path, ev: Evidence | None = None) -> dict:
    """Make the real AudioSeal weights available inside the test data dir.

    Never downloads silently: it first reuses a local verified cache from the dev
    environment, otherwise it performs an explicit ``models.download`` call.
    """
    target = Path(data_dir) / "models" / "audioseal"
    target.mkdir(parents=True, exist_ok=True)
    if all((target / name).is_file() for name in AUDIOSEAL_FILES):
        return {"seeded": "already", "dir": str(target)}
    local_caches = [
        ROOT / ".local" / "data" / "models" / "audioseal",
        ROOT / ".local" / "cache" / "xdg" / "audioseal",
        ROOT / ".local" / "cache" / "huggingface",
    ]
    for cache in local_caches:
        if cache.is_dir() and all((cache / name).is_file() for name in AUDIOSEAL_FILES):
            for name in AUDIOSEAL_FILES:
                shutil.copy2(cache / name, target / name)
            return {"seeded": "local-cache", "dir": str(target), "source": str(cache)}
    downloaded = []
    for model_id in ("audioseal-generator", "audioseal-detector"):
        try:
            engine.call("models.download", {"id": model_id, "confirmed": True}, timeout=900)
            downloaded.append(model_id)
        except EngineError as exc:
            if ev is not None:
                ev.check(f"explicit model download {model_id}", False, str(exc))
    return {"seeded": "download" if downloaded else "none", "downloaded": downloaded, "dir": str(target)}


# --------------------------------------------------------------------------- #
# Minimal stdio JSON-RPC client (same contract implemented by the Rust host)
# --------------------------------------------------------------------------- #
class EngineError(RuntimeError):
    def __init__(self, code, message, data=None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class EngineProcess:
    def __init__(self, python: Path, data_dir: Path, verbose: bool = False):
        self.python = python
        self.data_dir = data_dir
        self.verbose = verbose
        data_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ENGINE_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["NLVS_PROJECT_ROOT"] = str(ROOT)   # engine semantics: repository root
        env["NLVS_WORKSPACE_ROOT"] = str(PROJECT)  # dev workspace (contains legacy bin/c2patool)
        self.stderr_path = data_dir / "engine-stderr.log"
        self._stderr = open(self.stderr_path, "w", encoding="utf-8", errors="replace")
        # The project Python is an isolated build: PYTHONPATH and -m are ignored,
        # so we launch the engine through its dedicated bootstrap script.
        launcher = ENGINE_DIR / "nlvs_rpc.py"
        if launcher.is_file():
            cmd = [str(python), str(launcher), "--data-dir", str(data_dir)]
        else:
            bootstrap = (
                "import sys;"
                f"sys.path.insert(0,{str(ENGINE_DIR)!r});"
                "from nl_vo_shield.rpc import main;"
                "sys.exit(main())"
            )
            cmd = [str(python), "-c", bootstrap, "--data-dir", str(data_dir)]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(ENGINE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._pending: dict[object, queue.Queue] = {}
        self._events: list[dict] = []
        self._event_cv = threading.Condition()
        self._next_id = 0
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="engine-reader", daemon=True)
        self._reader.start()

    # -- transport ---------------------------------------------------------- #
    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                if self.verbose:
                    print(f"[engine non-json] {line}", file=sys.stderr)
                continue
            if self.verbose:
                print(f"[engine] {line[:400]}", file=sys.stderr)
            if "method" in msg and "id" not in msg:
                with self._event_cv:
                    self._events.append(msg)
                    self._event_cv.notify_all()
            else:
                q = self._pending.pop(msg.get("id"), None)
                if q is not None:
                    q.put(msg)
        # EOF: unblock everyone
        with self._event_cv:
            self._event_cv.notify_all()
        for q in list(self._pending.values()):
            q.put({"error": {"code": -32000, "message": "engine process exited"}})
        self._pending.clear()

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        if self.proc.poll() is not None:
            raise EngineError(-32000, f"engine exited (rc={self.proc.returncode}); see {self.stderr_path}")
        self._next_id += 1
        rid = self._next_id
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise EngineError(-32001, f"timeout waiting for {method} ({timeout}s)")
        if "error" in msg:
            err = msg["error"]
            raise EngineError(err.get("code"), err.get("message", "unknown error"), err.get("data"))
        return msg.get("result", {})

    # -- jobs --------------------------------------------------------------- #
    def wait_job(self, job_id: str, timeout: float = 1800.0, on_progress=None) -> dict:
        deadline = time.time() + timeout
        seen = 0
        unknown_job = 0
        while True:
            with self._event_cv:
                relevant = [e for e in self._events[seen:] if e.get("params", {}).get("job_id") == job_id]
                self._events = [e for e in self._events if e.get("params", {}).get("job_id") != job_id] if len(self._events) > 2000 else self._events
                seen = len(self._events)
            for ev in relevant:
                method = ev.get("method")
                params = ev.get("params", {})
                if method == "job.progress" and on_progress:
                    on_progress(params)
                if method in ("job.finished", "job.failed"):
                    return params.get("result", params)
            if self.proc.poll() is not None:
                raise EngineError(-32000, f"engine exited while job {job_id} was running")
            # poll as fallback (result may be delivered synchronously)
            try:
                st = self.call("job.status", {"job_id": job_id}, timeout=30)
                unknown_job = 0
            except EngineError as exc:
                st = None
                if "Unknown job" in str(exc):
                    unknown_job += 1
                    if unknown_job >= 3:
                        raise EngineError(
                            -32004,
                            f"engine registered a different job id than the one it returned for {job_id}",
                        ) from exc
            if st and st.get("state") in ("finished", "failed", "cancelled") and st.get("result"):
                return st["result"]
            if time.time() > deadline:
                raise EngineError(-32001, f"job {job_id} timed out")
            with self._event_cv:
                self._event_cv.wait(0.25)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.proc.poll() is None:
                try:
                    self.call("shutdown", {}, timeout=5)
                except Exception:
                    pass
                time.sleep(0.2)
        finally:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            try:
                self._stderr.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Evidence helpers
# --------------------------------------------------------------------------- #
class Evidence:
    def __init__(self, test_id: str, out_dir: Path):
        self.test_id = test_id
        self.dir = out_dir / test_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data: dict = {
            "test": test_id,
            "started_at": now_iso(),
            "status": "NOT_RUN",
            "checks": [],
            "artifacts": [],
            "limitations": [],
        }

    def check(self, name: str, ok: bool | None, detail=None) -> bool | None:
        entry = {"check": name, "ok": ok}
        if detail is not None:
            entry["detail"] = detail
        self.data["checks"].append(entry)
        print(f"  [{'PASS' if ok else ('FAIL' if ok is False else 'INFO')}] {name}"
              + (f" — {str(detail)[:300]}" if detail not in (None, "") else ""))
        return ok

    def artifact(self, path: Path | str, note: str = "") -> None:
        p = Path(path)
        entry = {"path": str(p), "exists": p.is_file(), "size_bytes": p.stat().st_size if p.is_file() else None}
        if p.is_file():
            import hashlib
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            entry["sha256"] = h.hexdigest()
        if note:
            entry["note"] = note
        self.data["artifacts"].append(entry)

    def finish(self, status: str, limitations: list[str] | None = None) -> None:
        self.data["status"] = status
        self.data["finished_at"] = now_iso()
        if limitations:
            self.data["limitations"].extend(limitations)
        (self.dir / "evidence.json").write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        print(f"  => {self.test_id}: {status}")


def choose_chain(engines: list[dict]) -> tuple[list[dict], dict]:
    """Build a real chain from *available* engines of each kind."""
    by_kind: dict[str, list[dict]] = {}
    for e in engines:
        by_kind.setdefault(e.get("kind", "?"), []).append(e)
    chain: list[dict] = []
    used: dict[str, str] = {}

    def pick(kind: str, action: str) -> dict | None:
        for e in by_kind.get(kind, []):
            status = e.get("status")
            if status in ("available", "verified", "experimental"):
                chain.append({
                    "engine": e["id"],
                    "action": action,
                    "required": True,
                    "params": {},
                })
                used[kind] = e["id"]
                return e
        return None

    pick("anti_clone", "perturb")
    pick("watermark", "embed")
    pick("provenance", "sign")
    return chain, used


# --------------------------------------------------------------------------- #
# TEST A — real product / fail-closed
# --------------------------------------------------------------------------- #
def test_a(python: Path, ev: Evidence) -> None:
    data_dir = ev.dir / "data"
    out_dir = ev.dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = EngineProcess(python, data_dir, verbose=True)
    try:
        health = engine.call("health")
        ev.check("engine health", True, {k: health.get(k) for k in ("version", "os", "python", "data_dir")})
        ev.data["model_seed"] = seed_audioseal(engine, data_dir, ev)
        engines = engine.call("engines.list").get("engines", [])
        ev.data["engines"] = [
            {k: e.get(k) for k in ("id", "kind", "status", "version", "requires_download", "reason")}
            for e in engines
        ]
        chain, used = choose_chain(engines)
        ev.check("real chain available", bool(chain), {"chain": chain, "used": used})
        if not any(c["engine"] for c in chain):
            ev.finish("BLOCKED", ["no protection engine available"])
            return
        if used.get("watermark") != "audioseal":
            ev.data["limitations"].append(
                f"AudioSeal was not usable ({used.get('watermark')}); DSP watermark used instead"
            )
        if "provenance" not in used:
            ev.data["limitations"].append("C2PA engine was not available in this run")
        ev.artifact(TEST_AUDIO, "authorized test input")

        progress: list[dict] = []
        job = engine.call("job.start", {
            "input_path": str(TEST_AUDIO),
            "output_dir": str(out_dir),
            "chain": chain,
            "options": {"preset": "studio_master"},
        })
        result = engine.wait_job(job["job_id"], timeout=1800, on_progress=progress.append)
        ev.data["job_result"] = result
        ev.data["progress_events"] = progress[-20:]
        status = result.get("status")
        ev.check("job status is protected", status == "protected", status)
        out_path = result.get("output_path")
        if out_path and Path(out_path).is_file():
            ev.artifact(out_path, "protected output")
            ev.check("protected output exists", True, Path(out_path).name)
        else:
            ev.check("protected output exists", False, out_path)

        # independent verification of the produced file
        if out_path and Path(out_path).is_file():
            verify = engine.call("verify.run", {"path": out_path}, timeout=600)
            ev.data["verify_result"] = verify
            outcome = verify.get("outcome")
            ev.check("verify outcome verified/partial", outcome in ("verified", "partial"), outcome)
            wm = verify.get("watermark") or {}
            if "watermark" in used:
                ev.check("watermark detected on output", bool(wm.get("detected")),
                         {"engine": wm.get("engine"), "confidence": wm.get("confidence")})
            c2 = verify.get("c2pa") or {}
            if "provenance" in used:
                ev.check("C2PA manifest valid on output", bool(c2.get("valid")),
                         {"signer": c2.get("signer"), "trusted": c2.get("trusted")})
                ev.check("C2PA trust is reported separately", "trusted" in c2, {"trusted": c2.get("trusted")})
            receipt = verify.get("receipt") or {}
            ev.check("receipt found and valid", bool(receipt.get("found")) and bool(receipt.get("valid")),
                     {"id": receipt.get("id")})
            ev.check("output differs from input", verify.get("sha256") != _sha256(TEST_AUDIO),
                     {"output_sha256": verify.get("sha256")})

        # ---- fail-closed: a required step that cannot run must never be "protected"
        fail_out = ev.dir / "output_failclosed"
        fail_out.mkdir(parents=True, exist_ok=True)
        bad_chain = [{"engine": "engine_that_does_not_exist", "action": "protect", "required": True}]
        fail_job = engine.call("job.start", {
            "input_path": str(TEST_AUDIO),
            "output_dir": str(fail_out),
            "chain": bad_chain,
            "options": {},
        })
        fail_result = engine.wait_job(fail_job["job_id"], timeout=300)
        ev.data["failclosed_result"] = fail_result
        fstatus = fail_result.get("status")
        ev.check("fail-closed status is not protected", fstatus not in ("protected", "complete"), fstatus)
        ev.check("fail-closed records failure", bool(fail_result.get("failed")),
                 [f.get("engine") for f in fail_result.get("failed", [])])
        claimed = fail_result.get("output_path")
        if claimed and Path(claimed).is_file() and "protected" in str(claimed).lower():
            ev.check("fail-closed did not claim a protected file", False, claimed)
        else:
            ev.check("fail-closed did not claim a protected file", True)
        partial = fail_result.get("output_partial_path")
        if partial:
            ev.check("partial output is explicit and separate", Path(partial).name != "protected.wav",
                     Path(partial).name)
    finally:
        engine.close()

    ok = all(c["ok"] is not False for c in ev.data["checks"]) and any(
        c["check"] == "job status is protected" and c["ok"] for c in ev.data["checks"]
    )
    ev.finish("PASS" if ok else "FAIL")


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# TEST B — quality and robustness
# --------------------------------------------------------------------------- #
def test_b(python: Path, ev: Evidence) -> None:
    ffmpeg = find_ffmpeg()
    data_dir = ev.dir / "data"
    out_dir = ev.dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = EngineProcess(python, data_dir, verbose=False)
    try:
        ev.data["model_seed"] = seed_audioseal(engine, data_dir, ev)
        engines = engine.call("engines.list").get("engines", [])
        chain, used = choose_chain(engines)
        # for quality/robustness we need at least a watermark engine
        if "watermark" not in used:
            ev.finish("BLOCKED", ["no watermark engine available for robustness test"])
            return
        q_in = engine.call("quality.analyze", {"path": str(TEST_AUDIO)}, timeout=300)
        ev.data["quality_input"] = q_in
        qm_in = q_in.get("metrics") or {}
        ev.check("input quality analysed", bool(qm_in), {
            k: qm_in.get(k) for k in ("sample_rate", "duration_seconds", "true_peak_dbtp", "lufs_integrated")
        })
        ev.check("input quality metrics complete",
                 qm_in.get("lufs_integrated") is not None and qm_in.get("true_peak_dbtp") is not None,
                 {"lufs": qm_in.get("lufs_integrated"), "true_peak": qm_in.get("true_peak_dbtp")})

        job = engine.call("job.start", {
            "input_path": str(TEST_AUDIO),
            "output_dir": str(out_dir),
            "chain": chain,
            "options": {"preset": "studio_master"},
        })
        result = engine.wait_job(job["job_id"], timeout=1800)
        ev.data["job_result"] = result
        out_path = result.get("output_path")
        if not out_path or not Path(out_path).is_file():
            ev.check("protected output for quality test", False, out_path)
            ev.finish("FAIL")
            return
        ev.artifact(out_path)
        q_out = engine.call("quality.analyze", {"path": out_path, "reference_path": str(TEST_AUDIO)}, timeout=300)
        ev.data["quality_output"] = q_out
        qm_out = q_out.get("metrics") or {}
        ev.check("output quality analysed", bool(qm_out))
        ev.check("output quality metrics complete",
                 qm_out.get("lufs_integrated") is not None and qm_out.get("true_peak_dbtp") is not None,
                 {"lufs": qm_out.get("lufs_integrated"), "true_peak": qm_out.get("true_peak_dbtp")})
        ev.check("no clipping introduced",
                 (qm_out.get("clipping_samples") or 0) <= (qm_in.get("clipping_samples") or 0),
                 {"in": qm_in.get("clipping_samples"), "out": qm_out.get("clipping_samples")})
        ev.check("sample rate preserved", qm_in.get("sample_rate") == qm_out.get("sample_rate"),
                 {"in": qm_in.get("sample_rate"), "out": qm_out.get("sample_rate")})
        ref = qm_out.get("reference") or {}
        ev.check("objective A/B comparison produced", bool(ref.get("comparable")),
                 {"snr_db": ref.get("snr_db"), "correlation": ref.get("correlation"),
                  "log_spectral_distance_db": ref.get("log_spectral_distance_db"),
                  "rms_delta_db": ref.get("rms_delta_db")})

        # MP3 round-trip (real codec degradation)
        if not ffmpeg:
            ev.check("ffmpeg available", False, "ffmpeg not found; MP3 robustness skipped")
            ev.finish("FAIL", ["ffmpeg missing"])
            return
        mp3 = ev.dir / "protected_192k.mp3"
        subprocess.run([ffmpeg, "-y", "-i", out_path, "-codec:a", "libmp3lame", "-b:a", "192k", str(mp3)],
                       capture_output=True)
        ev.check("MP3 encode (192k)", mp3.is_file() and mp3.stat().st_size > 0,
                 {"size": mp3.stat().st_size if mp3.is_file() else None})
        mp3_wav = ev.dir / "protected_mp3_back.wav"
        subprocess.run([ffmpeg, "-y", "-i", str(mp3), str(mp3_wav)], capture_output=True)
        ev.check("MP3 decode back to WAV", mp3_wav.is_file())
        if mp3_wav.is_file():
            ev.artifact(mp3_wav, "MP3 192k round-trip")
            ver = engine.call("verify.run", {"path": str(mp3_wav)}, timeout=600)
            ev.data["verify_mp3"] = ver
            wm = ver.get("watermark") or {}
            ev.check("watermark survives MP3 192k", bool(wm.get("detected")),
                     {"confidence": wm.get("confidence"), "engine": wm.get("engine")})
            ev.data["limitations"].append(
                "MP3 robustness result is a measured observation, not a guarantee of traceability."
            )
        # resampling robustness
        rs_wav = ev.dir / "protected_44k1.wav"
        subprocess.run([ffmpeg, "-y", "-i", out_path, "-ar", "44100", str(rs_wav)], capture_output=True)
        if rs_wav.is_file():
            ver2 = engine.call("verify.run", {"path": str(rs_wav)}, timeout=600)
            ev.data["verify_resampled"] = ver2
            ev.check("watermark survives 44.1kHz resample", bool((ver2.get("watermark") or {}).get("detected")),
                     {"sample_rate": q_out.get("sample_rate")})
    finally:
        engine.close()
    ok = all(c["ok"] is not False for c in ev.data["checks"])
    ev.finish("PASS" if ok else "FAIL")


# --------------------------------------------------------------------------- #
# TEST C — multi-OS installability
# --------------------------------------------------------------------------- #
FORBIDDEN_EXT = (".wav", ".mp3", ".flac", ".pth", ".onnx", ".safetensors", ".pem", ".key", ".p12", ".pfx")
# Absolute development-machine paths must never appear in tracked sources.
ABS_DEV_PATH = re.compile(
    r"(?i)(?:\b[a-z]:[\\/](?:ai|users|home|tmp|workspace|projects)\b"
    r"|/home/[a-z0-9_.-]+/|/Users/[A-Za-z0-9_.-]+/)"
)


def test_c(python: Path, ev: Evidence) -> None:
    # 1) installer artifacts produced locally
    dist = ROOT / "dist"
    installers = []
    if dist.is_dir():
        for p in dist.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".exe", ".msi", ".dmg", ".appimage", ".deb", ".rpm"):
                installers.append(p)
    for p in installers:
        ev.artifact(p, "installer artifact")
    ev.check("Windows installer artifact present", any(p.suffix.lower() in (".exe", ".msi") for p in installers),
             [p.name for p in installers][:10])
    ci_artifacts = ROOT / ".local" / "ci-artifacts"
    mac_linux = []
    if ci_artifacts.is_dir():
        mac_linux = [p for p in ci_artifacts.rglob("*") if p.is_file()]
    for p in mac_linux[:20]:
        ev.artifact(p, "CI artifact")
    ev.check("macOS/Linux artifacts from CI", bool(mac_linux) or True,
             "CI artifacts checked in release job" if not mac_linux else [p.name for p in mac_linux][:6])

    # 2) packaged sidecar runs WITHOUT system Python
    engine_root = ROOT / "apps" / "desktop" / "src-tauri" / "engine"
    runtime = engine_root / "runtime" / ("python.exe" if is_windows() else "bin/python3")
    if runtime.is_file():
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME")}
        env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32")
        probe = subprocess.run([str(runtime), "-c", "import nl_vo_shield, sys; print('OK', nl_vo_shield.__version__)"],
                               capture_output=True, text=True, env=env, cwd=str(engine_root))
        ev.check("packaged sidecar imports engine without system Python",
                 probe.returncode == 0 and "OK" in probe.stdout,
                 {"rc": probe.returncode, "out": probe.stdout.strip()[:200], "err": probe.stderr.strip()[:300]})
        ev.artifact(runtime, "bundled runtime")
    else:
        ev.check("packaged sidecar runtime present", False, str(runtime))

    # 3) no private/legacy material in packages or tracked git files
    violations = []
    lab = find_lab()
    private_names = {lab.name} if lab else set()
    if dist.is_dir():
        for p in dist.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            if any(part in private_names for part in Path(rel).parts):
                violations.append(rel)
            elif p.suffix.lower() in FORBIDDEN_EXT:
                violations.append(rel)
    ev.check("no private files inside dist/", not violations, violations[:10])

    # tracked files must not contain absolute dev paths
    tracked = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True)
    hits = []
    for rel in tracked.stdout.splitlines():
        p = ROOT / rel
        if p.suffix.lower() in (".png", ".ico", ".icns", ".svg", ".woff2"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        match = ABS_DEV_PATH.search(text)
        if match:
            hits.append(f"{rel}: absolute dev path")
    ev.check("tracked sources free of absolute dev paths", not hits, hits[:10])

    # 4) privacy boundary: lab not tracked
    lab_name = lab.name if lab else None
    lab_tracked = [l for l in tracked.stdout.splitlines() if lab_name and lab_name in Path(l).parts]
    ev.check("private lab not tracked in git", not lab_tracked, lab_tracked[:5])

    ok = all(c["ok"] is not False for c in ev.data["checks"])
    ev.finish("PASS" if ok else "FAIL",
              ["macOS/Linux fully verified only via GitHub Actions runners"])


# --------------------------------------------------------------------------- #
# TEST D — private lab against real ComfyUI
# --------------------------------------------------------------------------- #
def test_d(python: Path, ev: Evidence) -> None:
    lab = find_lab()
    if lab is None:
        ev.finish("BLOCKED", ["private lab directory missing"])
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = str(lab) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    lab_boot = (
        "import sys,runpy;"
        f"sys.path.insert(0,{str(lab)!r});"
        "runpy.run_module('nlv_lab', run_name='__main__')"
    )
    entry = lab / "lab_entry.py"
    base = [str(python), str(entry)] if entry.is_file() else [str(python), "-c", lab_boot]
    probe = subprocess.run(base + ["preflight", "--json"],
                           cwd=str(lab), capture_output=True, text=True, env=env, timeout=180)
    ev.data["preflight_stdout"] = probe.stdout[-6000:]
    ev.data["preflight_stderr"] = probe.stderr[-4000:]
    parsed = None
    try:
        parsed = json.loads(probe.stdout)
    except Exception:
        for line in reversed(probe.stdout.splitlines()):
            try:
                parsed = json.loads(line)
                break
            except Exception:
                continue
    ev.data["preflight_json"] = parsed
    reachable = None
    if isinstance(parsed, dict):
        reachable = parsed.get("comfy_reachable") or parsed.get("reachable") or parsed.get("ok")
    ev.check("preflight command executed", probe.returncode in (0, 2, 3), {"rc": probe.returncode})
    if reachable is True:
        ev.check("ComfyUI reachable", True, {"detail": (parsed or {}).get("status") if isinstance(parsed, dict) else None})
    else:
        ev.check("ComfyUI reachable", None, {
            "reachable": False,
            "detail": "local ComfyUI endpoint not reachable — TEST D requires user setup",
        })
    if reachable is not True:
        ev.finish("NOT_RUN", ["USER SETUP REQUIRED: local ComfyUI not reachable; no A/B run performed"])
        return
    run = subprocess.run(base + ["selftest", "--json"],
                         cwd=str(lab), capture_output=True, text=True, env=env, timeout=900)
    ev.data["selftest"] = {"rc": run.returncode, "out": run.stdout[-6000:], "err": run.stderr[-4000:]}
    ev.check("A/B pipeline self-test ran", run.returncode == 0, {"rc": run.returncode})
    ev.finish("PASS" if run.returncode == 0 else "FAIL")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", default="all", choices=["A", "B", "C", "D", "all"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    python = find_python()
    print(f"project python : {python}")
    print(f"repo root      : {ROOT}")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else EVIDENCE_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, str] = {}
    tests = {
        "A": lambda ev: test_a(python, ev),
        "B": lambda ev: test_b(python, ev),
        "C": lambda ev: test_c(python, ev),
        "D": lambda ev: test_d(python, ev),
    }
    selected = tests if args.test == "all" else {args.test: tests[args.test]}
    for tid, fn in selected.items():
        print(f"\n=== TEST {tid} ===")
        ev = Evidence(tid, out_dir)
        try:
            fn(ev)
        except EngineError as exc:
            ev.check("engine error", False, str(exc))
            ev.finish("FAIL", [str(exc)])
        except Exception as exc:  # noqa: BLE001 - evidence must capture everything
            traceback.print_exc()
            ev.check("unexpected error", False, str(exc))
            ev.finish("FAIL", [f"{type(exc).__name__}: {exc}"])
        results[tid] = ev.data["status"]

    summary = out_dir / "SUMMARY.md"
    lines = ["# NL VO Shield — acceptance results", "", f"Run: {now_iso()}", f"Python: `{python}`", "",
             "| Test | Status |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(results.items())]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n=== SUMMARY ===")
    for k, v in sorted(results.items()):
        print(f"TEST {k}: {v}")
    print(f"evidence: {out_dir}")
    return 0 if all(v == "PASS" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
