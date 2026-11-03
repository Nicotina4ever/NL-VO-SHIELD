"""Command line interface (`python -m nl_vo_shield.cli ...`).

Exit codes:
* 0  success (``protect`` only when status == protected)
* 2  fail-closed result (partial/failed/indeterminate/not_detected)
* 1  usage/internal error (structured JSON on stderr)
* 3  doctor found a failed check
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .context import AppContext
from .doctor import run_doctor
from .errors import NlvsError
from .pipeline import run_batch, run_job
from .quality import analyze as analyze_quality
from .util import dumps, full_id, json_safe


def _emit(data: Any, compact: bool) -> None:
    print(dumps(data, indent=None if compact else 2))


def _emit_error(message: str, *, code: str = "ERROR", detail: dict[str, Any] | None = None,
                compact: bool = False) -> None:
    print(
        dumps({"status": "error", "error": {"code": code, "message": message, "detail": detail or {}}},
              indent=None if compact else 2),
        file=sys.stderr,
    )


def _progress_printer(prefix: str = "") -> Any:
    state = {"last": -10}

    def callback(step: str, step_index: int, pct: int | None, message: str) -> None:
        if pct is not None and pct - state["last"] >= 5:
            state["last"] = pct
            print(f"{prefix}[{pct:3d}%] {step}: {message}", file=sys.stderr, flush=True)

    return callback


def _chain_from_args(ctx: AppContext, args: argparse.Namespace) -> tuple[list[dict[str, Any]], str | None]:
    from .errors import PipelineError

    chain: list[dict[str, Any]] | None = None
    if getattr(args, "chain", None):
        try:
            parsed = json.loads(args.chain)
        except json.JSONDecodeError as exc:
            raise PipelineError(f"--chain is not valid JSON: {exc}") from exc
        chain = parsed.get("chain") if isinstance(parsed, dict) else parsed
    elif getattr(args, "chain_file", None):
        raw = Path(args.chain_file).read_text(encoding="utf-8")
        parsed = json.loads(raw)
        chain = parsed.get("chain") if isinstance(parsed, dict) else parsed
    if chain is not None:
        if not isinstance(chain, list):
            raise PipelineError("The protection chain must be a JSON list of steps")
        return chain, getattr(args, "preset", None)
    preset_id = getattr(args, "preset", None) or ctx.settings_public().get("default_preset", "studio_master")
    chain, preset = ctx.preset_chain(str(preset_id))
    if preset is None:
        raise PipelineError(f"Unknown preset: {preset_id}")
    return chain, str(preset_id)


def _job_options(args: argparse.Namespace) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key in ("author", "usage_terms", "copyright", "payload_hex", "profile_id"):
        value = getattr(args, key, None)
        if value:
            options[key] = value
    return options


# --------------------------------------------------------------------------- commands
def cmd_protect(ctx: AppContext, args: argparse.Namespace) -> int:
    chain, preset = _chain_from_args(ctx, args)
    input_path = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir else input_path.parent
    result = run_job(
        ctx,
        job_id=full_id(),
        input_path=input_path,
        output_dir=output_dir,
        chain=chain,
        options=_job_options(args),
        preset=preset,
        progress=None if args.no_progress else _progress_printer(),
    )
    _emit(result, args.compact)
    return 0 if result.get("status") == "protected" else 2


def cmd_verify(ctx: AppContext, args: argparse.Namespace) -> int:
    from .verify import verify_file

    result = verify_file(ctx, args.input, receipts_dir=args.receipts_dir)
    _emit(result, args.compact)
    return 0 if result.get("outcome") == "verified" else 2


def cmd_quality(ctx: AppContext, args: argparse.Namespace) -> int:
    from .audio_io import load_audio

    buffer, info = load_audio(args.input)
    reference = None
    if args.reference:
        reference, _ = load_audio(args.reference)
    metrics = analyze_quality(buffer, reference)
    _emit({"path": str(args.input), "format": info.to_dict(), "metrics": metrics}, args.compact)
    return 0


def cmd_batch(ctx: AppContext, args: argparse.Namespace) -> int:
    chain, preset = _chain_from_args(ctx, args)
    result = run_batch(
        ctx,
        job_id=full_id(),
        inputs=list(args.inputs),
        output_dir=args.output_dir,
        chain=chain,
        options=_job_options(args),
        preset=preset,
        progress=None if args.no_progress else _progress_printer(),
    )
    _emit(result, args.compact)
    return 0 if result.get("status") == "protected" else 2


def cmd_watch(ctx: AppContext, args: argparse.Namespace) -> int:
    """Polling folder watcher: protect new audio files as they appear.

    Useful for DAW export folders. No integration with Reaper is implemented:
    the user points it at any folder (documented in the README).
    """
    import time

    chain, preset = _chain_from_args(ctx, args)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not input_dir.is_dir():
        raise NlvsError(f"Watch input directory not found: {input_dir}", code="WATCH_DIR_NOT_FOUND")
    output_dir.mkdir(parents=True, exist_ok=True)
    extensions = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus"}
    seen: set[str] = set()
    if args.skip_existing:
        for candidate in input_dir.iterdir():
            if candidate.is_file() and candidate.suffix.lower() in extensions:
                seen.add(str(candidate.resolve()))
    processed = 0
    failures = 0
    try:
        while True:
            pending = sorted(
                candidate
                for candidate in input_dir.iterdir()
                if candidate.is_file()
                and candidate.suffix.lower() in extensions
                and str(candidate.resolve()) not in seen
            )
            for path in pending:
                resolved = str(path.resolve())
                print(f"[watch] new file: {path.name}", file=sys.stderr, flush=True)
                result = run_job(
                    ctx,
                    job_id=full_id(),
                    input_path=path,
                    output_dir=output_dir,
                    chain=chain,
                    options=_job_options(args),
                    preset=preset,
                    progress=None,
                )
                processed += 1
                seen.add(resolved)
                if result.get("status") == "protected":
                    print(
                        f"[watch] protected: {path.name} -> {result.get('output_path')}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    failures += 1
                    reasons = [item.get("error") for item in result.get("failed") or []]
                    print(
                        f"[watch] {result.get('status')}: {path.name} (not protected) {reasons}",
                        file=sys.stderr,
                        flush=True,
                    )
            if args.once:
                break
            time.sleep(max(0.2, float(args.interval)))
    except KeyboardInterrupt:
        print("[watch] stopped by user", file=sys.stderr, flush=True)
    if args.once:
        return 0 if failures == 0 else 2
    return 0


def cmd_doctor(ctx: AppContext, args: argparse.Namespace) -> int:
    report = run_doctor(ctx)
    _emit(report, args.compact)
    return 3 if report["summary"]["status"] == "fail" else 0


def cmd_engines(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.install_hint:
        _emit(ctx.install_hint(args.install_hint), args.compact)
        return 0
    _emit({"engines": ctx.engine_infos()}, args.compact)
    return 0


# --------------------------------------------------------------------------- lab
def _lab(ctx: AppContext):
    from .qwen_lab import QwenLab

    return QwenLab(ctx.data_dir, ctx.models)


def _lab_progress() -> Any:
    state = {"last": -10}

    def callback(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
        if pct is None:
            if message:
                print(f"[lab] {message}", file=sys.stderr, flush=True)
            return
        if pct - state["last"] >= 2 or pct == 100:
            state["last"] = pct
            print(f"[lab {pct:3d}%] {message}", file=sys.stderr, flush=True)

    return callback


def cmd_lab(ctx: AppContext, args: argparse.Namespace) -> int:
    lab = _lab(ctx)
    action = args.lab_command
    if action == "status":
        _emit({"lab": lab.status()}, args.compact)
        return 0
    if action == "verify":
        report = lab.verify(deep=bool(args.deep))
        _emit({"lab": report}, args.compact)
        return 0 if report.get("ok") else 2
    if action == "install-engine":
        result = lab.install_engine(args.device, progress=_lab_progress())
        _emit({"lab": result}, args.compact)
        return 0 if result.get("ok") else 2
    if action == "uninstall-engine":
        _emit({"lab": lab.uninstall_engine(remove_models=bool(args.remove_models))}, args.compact)
        return 0
    if action == "install-model":
        result = lab.install_model(
            args.id, source=args.source, local_path=args.local_path, progress=_lab_progress()
        )
        _emit({"lab": result}, args.compact)
        return 0
    if action == "uninstall-model":
        _emit({"lab": lab.uninstall_model(args.id)}, args.compact)
        return 0
    if action == "install-whisper":
        result = lab.install_whisper(
            args.size, local_path=args.local_path, progress=_lab_progress()
        )
        _emit({"lab": result}, args.compact)
        return 0
    if action == "uninstall-whisper":
        _emit({"lab": lab.uninstall_whisper(args.size)}, args.compact)
        return 0
    if action == "transcribe":
        result = lab.transcribe(
            args.audio, model=args.model, language=args.language, device=args.device,
            progress=_lab_progress(),
        )
        _emit({"transcription": result}, args.compact)
        return 0
    if action == "run":
        result = lab.run(
            original=args.original,
            protected=args.protected,
            text=args.text,
            ref_text=args.ref_text,
            model_id=args.model,
            language=args.language,
            seed=args.seed,
            device=args.device,
            x_vector_only=bool(args.x_vector_only),
            output_dir=args.output_dir,
            progress=_lab_progress(),
        )
        _emit({"lab": result}, args.compact)
        return 0
    raise NlvsError(f"Unknown lab command: {action}")


def cmd_models(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.models_command == "list":
        models = ctx.models.list(verify_hash=not args.no_hash)
        _emit(
            {
                "models": models,
                "models_dir": ctx.settings["models_dir_resolved"],
                "cache_dir": ctx.settings["cache_dir_resolved"],
                "total_bytes": sum(item.get("size_bytes") or 0 for item in models if item.get("present")),
            },
            args.compact,
        )
        return 0
    if args.models_command == "download":
        if not args.yes:
            _emit_error(
                "Model downloads must be explicit: re-run with --yes to confirm this download",
                code="CONFIRMATION_REQUIRED",
                detail={"model_id": args.id},
                compact=args.compact,
            )
            return 1
        state = {"last": -10}

        def progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
            if pct is None:
                return
            if pct - state["last"] >= 5 or pct == 100:
                state["last"] = pct
                print(f"[{pct:3d}%] {message}", file=sys.stderr, flush=True)

        result = ctx.models.download(args.id, progress=progress)
        _emit(result, args.compact)
        return 0
    if args.models_command == "delete":
        _emit(ctx.models.delete(args.id), args.compact)
        return 0
    if args.models_command == "prune":
        _emit(ctx.models.prune(), args.compact)
        return 0
    raise NlvsError(f"Unknown models command: {args.models_command}")


def cmd_cache(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.cache_command == "size":
        _emit(ctx.cache.size(), args.compact)
        return 0
    if args.cache_command == "clear":
        _emit(ctx.cache.clear(), args.compact)
        return 0
    raise NlvsError(f"Unknown cache command: {args.cache_command}")


def cmd_receipts(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.receipts_command == "list":
        _emit({"receipts": ctx.receipts.list(limit=args.limit)}, args.compact)
        return 0
    if args.receipts_command == "get":
        receipt = ctx.receipts.get(args.id)
        if receipt is None:
            _emit_error(f"Receipt not found: {args.id}", code="RECEIPT_NOT_FOUND", compact=args.compact)
            return 1
        _emit({"receipt": receipt}, args.compact)
        return 0
    if args.receipts_command == "verify":
        trust_mode = str(ctx.settings_public().get("receipt_trust_mode", "local"))
        result = ctx.receipts.verify(args.id, trust_mode=trust_mode)
        _emit(
            {"valid": bool(result.get("valid")), "reason": result.get("reason"),
             "receipt": result.get("receipt")},
            args.compact,
        )
        return 0 if result.get("valid") else 2
    raise NlvsError(f"Unknown receipts command: {args.receipts_command}")


def cmd_profiles(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.profiles_command == "list":
        _emit({"profiles": ctx.profiles.list()}, args.compact)
        return 0
    if args.profiles_command == "save":
        if not args.payload_json:
            _emit_error("profiles save requires --profile-json '<profile object>'", code="MISSING_ARGUMENT",
                        compact=args.compact)
            return 1
        profile = json.loads(args.payload_json)
        _emit({"profile": ctx.profiles.save(profile)}, args.compact)
        return 0
    if args.profiles_command == "delete":
        _emit({"deleted": ctx.profiles.delete(args.id)}, args.compact)
        return 0
    raise NlvsError(f"Unknown profiles command: {args.profiles_command}")


def cmd_presets(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.presets_command == "list":
        _emit({"presets": ctx.resolved_presets()}, args.compact)
        return 0
    if args.presets_command == "save":
        if not args.payload_json:
            _emit_error("presets save requires --preset-json '<preset object>'", code="MISSING_ARGUMENT",
                        compact=args.compact)
            return 1
        preset = json.loads(args.payload_json)
        _emit({"preset": ctx.presets.save(preset)}, args.compact)
        return 0
    if args.presets_command == "delete":
        _emit({"deleted": ctx.presets.delete(args.id)}, args.compact)
        return 0
    raise NlvsError(f"Unknown presets command: {args.presets_command}")


def cmd_settings(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.settings_command == "get":
        _emit({"settings": ctx.settings_public()}, args.compact)
        return 0
    if args.settings_command == "set":
        if not args.payload_json:
            _emit_error("settings set requires --settings-json '<settings object>'", code="MISSING_ARGUMENT",
                        compact=args.compact)
            return 1
        patch = json.loads(args.payload_json)
        _emit({"settings": ctx.save_settings(patch)}, args.compact)
        return 0
    raise NlvsError(f"Unknown settings command: {args.settings_command}")


def cmd_info(ctx: AppContext, args: argparse.Namespace) -> int:
    _emit(ctx.health(), args.compact)
    return 0


def cmd_rpc(ctx: AppContext, args: argparse.Namespace) -> int:
    from .rpc import RpcServer

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    return RpcServer(ctx).serve()


# --------------------------------------------------------------------------- parser
def _add_common_options(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """Common options, accepted both before and after the subcommand."""
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--data-dir", default=default, help="application data directory"
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="portable data dir next to the executable",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="single-line JSON output",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="JSON output (default; accepted for scripting symmetry)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nl_vo_shield",
        description="NL VO Shield engine — local audio protection, watermark, provenance, verification",
    )
    parser.add_argument("--version", action="version", version=f"NL VO Shield engine {__version__}")
    _add_common_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("protect", help="apply a fail-closed protection chain to one file")
    p.add_argument("input")
    p.add_argument("--output-dir", default=None)
    p.add_argument("--preset", default=None)
    p.add_argument("--chain", default=None, help="JSON chain (list of steps)")
    p.add_argument("--chain-file", default=None)
    p.add_argument("--author", default=None)
    p.add_argument("--usage-terms", default=None)
    p.add_argument("--copyright", default=None)
    p.add_argument("--payload-hex", default=None, help="16-bit watermark payload, e.g. 1a2b")
    p.add_argument("--profile-id", default=None)
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=cmd_protect)

    p = sub.add_parser("verify", help="verify watermark, C2PA, fingerprint and receipt")
    p.add_argument("input")
    p.add_argument("--receipts-dir", default=None)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("quality", help="objective audio quality metrics")
    p.add_argument("input")
    p.add_argument("--reference", default=None)
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("batch", help="protect several files with the same chain")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--preset", default=None)
    p.add_argument("--chain", default=None)
    p.add_argument("--chain-file", default=None)
    p.add_argument("--author", default=None)
    p.add_argument("--usage-terms", default=None)
    p.add_argument("--copyright", default=None)
    p.add_argument("--payload-hex", default=None)
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("doctor", help="read-only environment/model/tool diagnostics")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("watch", help="watch a folder and protect new audio files")
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--preset", default=None)
    p.add_argument("--chain", default=None)
    p.add_argument("--chain-file", default=None)
    p.add_argument("--author", default=None)
    p.add_argument("--usage-terms", default=None)
    p.add_argument("--copyright", default=None)
    p.add_argument("--payload-hex", default=None)
    p.add_argument("--interval", type=float, default=2.0, help="polling interval in seconds")
    p.add_argument("--once", action="store_true", help="process the current files once and exit")
    p.add_argument("--skip-existing", action="store_true",
                   help="ignore files already present when the watcher starts")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("engines", help="list engines and availability")
    p.add_argument("--install-hint", default=None, metavar="ENGINE_ID")
    p.set_defaults(func=cmd_engines)

    p = sub.add_parser("lab", help="Qwen3-TTS test laboratory (optional runtime, explicit installs)")
    lab_sub = p.add_subparsers(dest="lab_command", required=True)
    lab_sub.add_parser("status")
    lab_verify = lab_sub.add_parser("verify")
    lab_verify.add_argument("--deep", action="store_true")
    lab_engine = lab_sub.add_parser("install-engine")
    lab_engine.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    lab_engine = lab_sub.add_parser("uninstall-engine")
    lab_engine.add_argument("--remove-models", action="store_true")
    lab_model = lab_sub.add_parser("install-model")
    lab_model.add_argument("id")
    lab_model.add_argument("--source", default=None, choices=["download"])
    lab_model.add_argument("--local-path", default=None)
    lab_model = lab_sub.add_parser("uninstall-model")
    lab_model.add_argument("id")
    lab_whisper = lab_sub.add_parser("install-whisper")
    lab_whisper.add_argument("size", choices=["tiny", "base", "small", "medium", "large-v3"])
    lab_whisper.add_argument("--local-path", default=None)
    lab_whisper = lab_sub.add_parser("uninstall-whisper")
    lab_whisper.add_argument("size", choices=["tiny", "base", "small", "medium", "large-v3"])
    lab_transcribe = lab_sub.add_parser("transcribe")
    lab_transcribe.add_argument("audio")
    lab_transcribe.add_argument("--model", default="base",
                                choices=["tiny", "base", "small", "medium", "large-v3"])
    lab_transcribe.add_argument("--language", default=None)
    lab_transcribe.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    lab_run = lab_sub.add_parser("run")
    lab_run.add_argument("--original", required=True)
    lab_run.add_argument("--protected", required=True)
    lab_run.add_argument("--text", required=True)
    lab_run.add_argument("--ref-text", default=None)
    lab_run.add_argument("--model", default="qwen3-tts-0.6b")
    lab_run.add_argument("--language", default="Italian")
    lab_run.add_argument("--seed", type=int, default=0)
    lab_run.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    lab_run.add_argument("--x-vector-only", action="store_true")
    lab_run.add_argument("--output-dir", default=None)
    p.set_defaults(func=cmd_lab)

    p = sub.add_parser("models", help="model inventory and explicit downloads")
    models_sub = p.add_subparsers(dest="models_command", required=True)
    m = models_sub.add_parser("list")
    m.add_argument("--no-hash", action="store_true", help="skip SHA-256 verification of local files")
    m = models_sub.add_parser("download")
    m.add_argument("id")
    m.add_argument("--yes", action="store_true", help="required confirmation for network download")
    m = models_sub.add_parser("delete")
    m.add_argument("id")
    models_sub.add_parser("prune")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("cache", help="own cache size/clear")
    cache_sub = p.add_subparsers(dest="cache_command", required=True)
    cache_sub.add_parser("size")
    cache_sub.add_parser("clear")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("receipts", help="signed local receipts")
    receipts_sub = p.add_subparsers(dest="receipts_command", required=True)
    r = receipts_sub.add_parser("list")
    r.add_argument("--limit", type=int, default=200)
    r = receipts_sub.add_parser("get")
    r.add_argument("id")
    r = receipts_sub.add_parser("verify")
    r.add_argument("id")
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("profiles", help="rights/author profiles")
    profiles_sub = p.add_subparsers(dest="profiles_command", required=True)
    profiles_sub.add_parser("list")
    r = profiles_sub.add_parser("save")
    r.add_argument("--profile-json", dest="payload_json", required=True)
    r = profiles_sub.add_parser("delete")
    r.add_argument("id")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("presets", help="protection presets")
    presets_sub = p.add_subparsers(dest="presets_command", required=True)
    presets_sub.add_parser("list")
    r = presets_sub.add_parser("save")
    r.add_argument("--preset-json", dest="payload_json", required=True)
    r = presets_sub.add_parser("delete")
    r.add_argument("id")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("settings", help="application settings")
    settings_sub = p.add_subparsers(dest="settings_command", required=True)
    settings_sub.add_parser("get")
    r = settings_sub.add_parser("set")
    r.add_argument("--settings-json", dest="payload_json", required=True)
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("info", help="engine health/info")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("rpc", help="run the JSON-RPC stdio server")
    p.set_defaults(func=cmd_rpc)

    # Allow the common options after any subcommand (e.g. `doctor --json`,
    # `models list --compact`). argparse defaults are suppressed so options
    # given before the subcommand are not overwritten.
    def _apply_common_recursive(current: argparse.ArgumentParser) -> None:
        for action in current._actions:  # noqa: SLF001 - argparse has no public API
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for choice in action.choices.values():
                    _add_common_options(choice, suppress=True)
                    _apply_common_recursive(choice)

    _apply_common_recursive(parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ctx = AppContext(args.data_dir, portable=True if args.portable else None)
    except NlvsError as exc:
        _emit_error(str(exc), code=getattr(exc, "code", "ERROR"), detail=exc.detail,
                    compact=getattr(args, "compact", False))
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        _emit_error(f"{type(exc).__name__}: {exc}", compact=getattr(args, "compact", False))
        return 1
    try:
        return int(args.func(ctx, args))
    except NlvsError as exc:
        _emit_error(str(exc), code=getattr(exc, "code", "ERROR"), detail=exc.detail,
                    compact=getattr(args, "compact", False))
        return 1
    except json.JSONDecodeError as exc:
        _emit_error(f"Invalid JSON argument: {exc}", code="JSON_INVALID", compact=getattr(args, "compact", False))
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - defensive
        _emit_error(f"{type(exc).__name__}: {exc}", code="INTERNAL_ERROR",
                    compact=getattr(args, "compact", False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


