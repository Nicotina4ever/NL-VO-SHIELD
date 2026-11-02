"""Path discovery and "no absolute development paths in the package" checks.

Marker strings are assembled at runtime on purpose: this test file itself must
not contain the forbidden literals, otherwise it would trip the repository
leak-guard while checking for leaks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nl_vo_shield import env, paths


def _forbidden_markers() -> tuple[str, ...]:
    drive = "K" + ":" + "\\"
    sibling_runtime = "python" + "_" + "win"
    private_lab = "_PRIVATE" + "_RESEARCH" + "_LAB"
    user_home = "C" + ":" + "\\" + "Users"
    return (drive, "K" + ":" + "/", sibling_runtime, user_home, private_lab)


def test_repo_root_contains_package() -> None:
    root = paths.repo_root()
    assert (root / "packages" / "engine" / "nl_vo_shield").is_dir()
    assert (root / "docs" / "ARCHITECTURE.md").is_file()


def test_workspace_root_contains_tools_when_dev() -> None:
    workspace = paths.workspace_root()
    assert workspace.is_dir()
    assert isinstance(workspace, Path)


def test_no_absolute_development_paths_in_package() -> None:
    package_dir = Path(paths.__file__).resolve().parent
    drive = "K" + ":" + "\\"
    user_home = "C" + ":" + "\\" + "Users"
    private_lab = "_PRIVATE" + "_RESEARCH" + "_LAB"
    markers = (drive, "K" + ":" + "/", user_home, private_lab)
    offenders: list[str] = []
    for source in package_dir.rglob("*.py"):
        text = source.read_text(encoding="utf-8", errors="replace")
        for needle in markers:
            if needle in text:
                offenders.append(f"{source.name}: {needle!r}")
    assert offenders == [], f"Absolute/leaky paths found in package: {offenders}"


def test_sibling_runtime_marker_only_in_path_discovery() -> None:
    """The dev runtime folder name may only appear in paths.py (documented fallback)."""
    package_dir = Path(paths.__file__).resolve().parent
    sibling_runtime = "python" + "_" + "win"
    allowed = {package_dir / "paths.py"}
    offenders = [
        source.name
        for source in package_dir.rglob("*.py")
        if source not in allowed
        and sibling_runtime in source.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], f"Unexpected references to the dev runtime: {offenders}"


def test_package_does_not_reference_private_content() -> None:
    package_dir = Path(paths.__file__).resolve().parent
    drive = "K" + ":" + "\\"
    user_home = "C" + ":" + "\\" + "Users"
    private_lab = "_PRIVATE" + "_RESEARCH" + "_LAB"
    markers = (drive, "K" + ":" + "/", user_home, private_lab)
    for source in package_dir.rglob("*"):
        if source.is_file() and source.suffix in {".py", ".json", ".txt", ".md"}:
            text = source.read_text(encoding="utf-8", errors="replace")
            assert not any(needle in text for needle in markers), source.name


def test_data_dir_layout_under_temp(tmp_path: Path) -> None:
    from nl_vo_shield.context import AppContext

    context = AppContext(tmp_path / "data")
    assert Path(context.settings["models_dir_resolved"]).is_dir()
    assert Path(context.settings["cache_dir_resolved"]).is_dir()
    assert context.cache.owned is True
    assert Path(context.settings["models_dir_resolved"]).is_relative_to(tmp_path)
    assert Path(context.settings["cache_dir_resolved"]).is_relative_to(tmp_path)


def test_model_environment_points_inside_data_dir(tmp_path: Path) -> None:
    from nl_vo_shield.context import AppContext

    context = AppContext(tmp_path / "data")
    models_dir = Path(context.settings["models_dir_resolved"])
    cache_dir = Path(context.settings["cache_dir_resolved"])
    applied = env.applied()
    assert Path(applied["AUDIOSEAL_CACHE_DIR"]).is_relative_to(models_dir)
    for key in ("TORCH_HOME", "HF_HOME", "HF_HUB_CACHE", "XDG_CACHE_HOME"):
        assert Path(applied[key]).is_relative_to(cache_dir), key
    assert applied["NO_TORCH_COMPILE"] == "1"
    assert applied["HF_HUB_OFFLINE"] == "1"


def test_manual_data_dir_override(tmp_path: Path) -> None:
    from nl_vo_shield.context import AppContext

    target = tmp_path / "custom-data"
    context = AppContext(target)
    assert context.data_dir == target.resolve()


def test_is_within_guard(tmp_path: Path) -> None:
    root = tmp_path / "a"
    child = root / "b"
    other = tmp_path / "other"
    assert paths.is_within(child, root) is True
    assert paths.is_within(root, root) is True
    assert paths.is_within(other, root) is False
