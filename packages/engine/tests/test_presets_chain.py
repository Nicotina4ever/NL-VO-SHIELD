"""Presets as real 3-level chains, plus chain validation rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from nl_vo_shield import pipeline
from nl_vo_shield.errors import PipelineError
from nl_vo_shield.util import full_id

from .conftest import c2pa_available

LEVELS = {"public_distribution": "light", "professional_demo": "balanced",
          "studio_master": "strong", "custom": "custom"}


def test_resolved_presets_levels_and_chains(ctx) -> None:
    presets = {item["id"]: item for item in ctx.resolved_presets()}
    for preset_id, level in LEVELS.items():
        assert preset_id in presets
        preset = presets[preset_id]
        assert preset["level"] == level
        for key in ("name", "label", "description", "impact", "chain", "quality_warning"):
            assert key in preset, (preset_id, key)
    # Light level: watermark + C2PA, no perturbation.
    light_engines = [step["engine"] for step in presets["public_distribution"]["chain"]]
    assert "c2pa" in light_engines
    assert not any(engine == "spectral_cloak" for engine in light_engines)
    assert presets["public_distribution"]["watermark_engine"] in ("audioseal", "watermark_dsp")
    # Balanced: light perturbation.
    balanced = presets["professional_demo"]["chain"]
    cloak = next(step for step in balanced if step["engine"] == "spectral_cloak")
    assert cloak["params"]["strength"] == "low"
    # Strong: medium perturbation and an explicit quality warning.
    strong = presets["studio_master"]["chain"]
    cloak = next(step for step in strong if step["engine"] == "spectral_cloak")
    assert cloak["params"]["strength"] == "medium"
    assert presets["studio_master"]["quality_warning"] is True
    # Watermark placeholder is always resolved to a real engine.
    for preset in presets.values():
        for step in preset["chain"]:
            assert step["engine"] != "watermark_auto"


def test_preset_chain_resolution(ctx) -> None:
    chain, preset = ctx.preset_chain("public_distribution")
    assert preset is not None and chain
    assert next(step["engine"] for step in chain if step["action"] == "embed") == ctx.watermark_engine_id()
    missing, absent = ctx.preset_chain("nope")
    assert missing == [] and absent is None


def test_duplicate_steps_rejected(ctx, tone_wav: Path, tmp_path: Path) -> None:
    chain = [
        {"engine": "watermark_dsp", "action": "embed", "required": True},
        {"engine": "watermark_dsp", "action": "embed", "required": True},
    ]
    with pytest.raises(PipelineError) as error:
        pipeline.run_job(
            ctx, job_id=full_id(), input_path=tone_wav,
            output_dir=tmp_path / "out", chain=chain,
        )
    assert error.value.code == "CHAIN_DUPLICATE_STEP"


def test_two_anti_clone_engines_rejected(ctx, tone_wav: Path, tmp_path: Path) -> None:
    chain = [
        {"engine": "spectral_cloak", "action": "perturb", "required": True,
         "params": {"strength": "low"}},
        {"engine": "safespeech", "action": "perturb", "required": True},
    ]
    with pytest.raises(PipelineError) as error:
        pipeline.run_job(
            ctx, job_id=full_id(), input_path=tone_wav,
            output_dir=tmp_path / "out", chain=chain,
        )
    assert error.value.code == "CHAIN_INCOMPATIBLE_ANTICLONE"


def test_measurement_engine_cannot_be_a_protection_step(ctx, tone_wav: Path, tmp_path: Path) -> None:
    with pytest.raises(PipelineError):
        pipeline.run_job(
            ctx, job_id=full_id(), input_path=tone_wav,
            output_dir=tmp_path / "out",
            chain=[{"engine": "quality", "action": "embed", "required": True}],
        )


def test_explicit_chain_is_respected(ctx, tone_wav: Path, tmp_path: Path) -> None:
    if not c2pa_available(ctx):
        pytest.skip("c2patool not available")
    chain = [
        {"engine": "spectral_cloak", "action": "perturb", "required": False,
         "params": {"strength": "low", "seed": 7}},
        {"engine": "watermark_dsp", "action": "embed", "required": True},
        {"engine": "c2pa", "action": "sign", "required": True},
    ]
    result = pipeline.run_job(
        ctx, job_id=full_id(), input_path=tone_wav,
        output_dir=tmp_path / "out", chain=chain,
    )
    assert result["status"] == "protected"
    executed = [(item["engine"], item["action"]) for item in result["executed"]]
    assert executed == [
        ("spectral_cloak", "perturb"),
        ("watermark_dsp", "embed"),
        ("c2pa", "sign"),
    ]
    assert result["requested_engines"] == ["spectral_cloak", "watermark_dsp", "c2pa"]


@pytest.mark.parametrize("preset_id", ["public_distribution", "professional_demo", "studio_master"])
def test_each_level_runs_for_real(ctx, tone_wav: Path, tmp_path: Path, preset_id: str) -> None:
    if not c2pa_available(ctx):
        pytest.skip("c2patool not available: presets require C2PA")
    chain, preset = ctx.preset_chain(preset_id)
    assert preset is not None
    result = pipeline.run_job(
        ctx,
        job_id=full_id(),
        input_path=tone_wav,
        output_dir=tmp_path / preset_id,
        chain=chain,
        preset=preset_id,
    )
    assert result["status"] == "protected", result.get("failed")
    assert result["output_path"]
    assert Path(result["output_path"]).is_file()
    assert result["receipt_id"]
    engines = [item["engine"] for item in result["executed"]]
    assert "c2pa" in engines
    assert any(engine in ("audioseal", "watermark_dsp") for engine in engines)
    if preset_id == "public_distribution":
        assert "spectral_cloak" not in engines
    else:
        assert "spectral_cloak" in engines
