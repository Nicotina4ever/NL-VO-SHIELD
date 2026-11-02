"""Models, cache and settings: explicit downloads, data-dir confinement, safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from nl_vo_shield.cache import CacheManager
from nl_vo_shield.context import AppContext
from nl_vo_shield.errors import CacheError, ModelError, SettingsError


def test_models_inventory_contract(ctx) -> None:
    models = ctx.models.list(verify_hash=False)
    ids = {item["id"] for item in models}
    assert {"audioseal-generator", "audioseal-detector", "safespeech-weights"} <= ids
    for item in models:
        for key in ("id", "kind", "path", "size_bytes", "present", "source",
                    "sha256", "engine", "download_allowed"):
            assert key in item
        assert item["download_allowed"] is False if item["engine"] == "safespeech" else True


def test_models_are_inside_models_dir(ctx) -> None:
    models_dir = Path(ctx.settings["models_dir_resolved"])
    for item in ctx.models.list(verify_hash=False):
        if item["path"] and item["download_allowed"]:
            assert Path(item["path"]).is_relative_to(models_dir)


def test_unknown_model_download_rejected(ctx) -> None:
    with pytest.raises(ModelError) as error:
        ctx.models.download("not-a-real-model")
    assert error.value.code == "MODEL_UNKNOWN"


def test_external_model_download_rejected(ctx) -> None:
    with pytest.raises(ModelError) as error:
        ctx.models.download("safespeech-weights")
    assert error.value.code == "MODEL_DOWNLOAD_NOT_ALLOWED"


def test_external_model_delete_rejected(ctx) -> None:
    with pytest.raises(ModelError):
        ctx.models.delete("safespeech-weights")


def test_downloaded_model_is_checksum_verified(ctx) -> None:
    """Checksum/size live in the manifest; verify with the real files if present."""
    generator = next(item for item in ctx.models.list(verify_hash=True)
                     if item["id"] == "audioseal-generator")
    if not generator["present"]:
        pytest.skip("AudioSeal weights not downloaded in this data dir")
    assert generator["integrity"] == "ok"
    assert generator["sha256"] == generator["expected_sha256"]


def test_prune_is_safe(ctx) -> None:
    models_dir = Path(ctx.settings["models_dir_resolved"])
    stray = models_dir / "audioseal" / "leftover.part"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"partial download")
    result = ctx.models.prune()
    assert result["removed_files"] >= 1
    assert not stray.exists()


def test_cache_owned_and_clearable(ctx) -> None:
    assert ctx.cache.owned is True
    cache_dir = Path(ctx.settings["cache_dir_resolved"])
    (cache_dir / "torch" / "blob.bin").write_bytes(b"0" * 1024)
    size = ctx.cache.size()
    assert size["size_bytes"] >= 1024
    result = ctx.cache.clear()
    assert result["cleared"] is True
    assert not (cache_dir / "torch").exists()


def test_cache_never_clears_a_foreign_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    foreign_cache = data_dir / "cache"
    foreign_cache.mkdir(parents=True)
    user_file = foreign_cache / "user-document.txt"
    user_file.write_text("important user data", encoding="utf-8")
    context = AppContext(data_dir)
    assert context.cache.owned is False
    assert context.cache.size()["owned"] is False
    with pytest.raises(CacheError) as error:
        context.cache.clear()
    assert error.value.code == "CACHE_MARKER_MISSING"
    assert user_file.read_text(encoding="utf-8") == "important user data"


def test_cache_manager_does_not_claim_foreign_dir(tmp_path: Path) -> None:
    foreign = tmp_path / "pictures"
    foreign.mkdir()
    (foreign / "photo.jpg").write_bytes(b"jpeg")
    manager = CacheManager(foreign)
    manager.ensure()
    assert manager.owned is False
    with pytest.raises(CacheError):
        manager.clear()
    assert (foreign / "photo.jpg").is_file()


def test_settings_defaults_and_persistence(ctx) -> None:
    settings = ctx.settings_public()
    assert settings["models_dir_resolved"].endswith("models")
    assert settings["cache_dir_resolved"].endswith("cache")
    assert settings["portable"] is False
    updated = ctx.save_settings({"language": "en", "watermark_threshold": 0.6})
    assert updated["language"] == "en"
    assert updated["watermark_threshold"] == 0.6
    assert Path(ctx.data_dir, "settings.json").is_file()
    reloaded = ctx.settings_store.load()
    assert reloaded["language"] == "en"


def test_settings_reject_invalid_values(ctx) -> None:
    with pytest.raises(SettingsError):
        ctx.save_settings({"unknown_key": 1})
    with pytest.raises(SettingsError):
        ctx.save_settings({"watermark_threshold": 5})


def test_settings_models_dir_must_differ_from_cache_dir(ctx) -> None:
    same = str(Path(ctx.data_dir) / "same")
    with pytest.raises(SettingsError):
        ctx.save_settings({"models_dir": same, "cache_dir": same})


def test_settings_models_dir_relocation(ctx, tmp_path: Path) -> None:
    new_models = tmp_path / "models-elsewhere"
    updated = ctx.save_settings({"models_dir": str(new_models)})
    assert Path(updated["models_dir_resolved"]) == new_models.resolve()
    assert ctx.models.models_dir == new_models.resolve()
    entries = ctx.models.list(verify_hash=False)
    assert all(Path(item["path"]).is_relative_to(new_models.resolve())
               for item in entries if item["path"] and item["download_allowed"])
