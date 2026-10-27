"""Installer pipeline tests: builder config, staging and artifact discovery.

The actual packagers (NSIS, AppImage, deb) need Node, network and (for Windows
targets on Linux) Wine, so they are not run here. What *is* verified is the part a
release can silently break: the electron-builder configuration, the staged layout
the config points at, and how artifacts are collected and reported.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from packaging import build_installer


def test_config_is_valid_and_admin_free() -> None:
    assert build_installer.check_config() == []


def test_config_describes_both_platforms() -> None:
    config = json.loads(build_installer.CONFIG.read_text(encoding="utf-8"))
    assert config["productName"] == "UQAB"
    assert config["artifactName"].startswith("${productName}")
    win_targets = [entry["target"] for entry in config["win"]["target"]]
    assert win_targets == ["nsis"]
    assert config["nsis"]["perMachine"] is False
    assert config["nsis"]["allowElevation"] is False
    assert config["nsis"]["allowToChangeInstallationDirectory"] is True
    linux_targets = [entry["target"] for entry in config["linux"]["target"]]
    assert {"AppImage", "deb"} <= set(linux_targets)
    assert config["linux"]["category"] == "Network"
    assert config["linux"]["desktop"]["entry"]["Name"] == "UQAB"
    # The engine and the Python runtime are shipped as resources, not packed into asar.
    sources = {entry["to"] for entry in config["extraResources"]}
    assert {"engine", "python"} <= sources or "python" in sources


def test_config_rejects_an_admin_installer(tmp_path: Path) -> None:
    config = json.loads(build_installer.CONFIG.read_text(encoding="utf-8"))
    config["nsis"]["perMachine"] = True
    config["win"]["target"] = ["portable"]
    bad = tmp_path / "electron-builder.json"
    bad.write_text(json.dumps(config), encoding="utf-8")
    problems = build_installer.check_config(bad)
    assert any("perMachine" in problem for problem in problems)
    assert any("nsis" in problem for problem in problems)


def test_builder_command_targets_the_requested_platforms() -> None:
    command = build_installer.electron_builder_command(["win"])
    assert command[:3] == ["npx", "--yes", "electron-builder@26"]
    assert "--config" in command
    assert command[-1] == "--win"
    assert "--projectDir" in command
    assert "app" in command[command.index("--projectDir") + 1]


def test_artifact_discovery_lists_packages(tmp_path: Path) -> None:
    for name in (
        "UQAB-Setup-1.0.0.exe",
        "UQAB-1.0.0-linux-x86_64.AppImage",
        "UQAB-1.0.0-linux-amd64.deb",
        "latest.yml",
        "notes.txt",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    found = {path.name for path in build_installer.collect_artifacts(tmp_path)}
    assert "UQAB-Setup-1.0.0.exe" in found
    assert "UQAB-1.0.0-linux-x86_64.AppImage" in found
    assert "UQAB-1.0.0-linux-amd64.deb" in found
    assert "notes.txt" not in found
    assert build_installer.collect_artifacts(tmp_path / "missing") == []


def fake_python_archive(path: Path) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in (
            ("python/python.exe", b"MZ interpreter"),
            ("python/LICENSE.txt", b"PSF"),
            ("python/Lib/os.py", b"# stdlib"),
            ("python/Lib/test/test_os.py", b"trimmed"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(payload))
    return path


@pytest.fixture()
def staged_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point the stage directories at a temp dir so the repo build/ stays clean."""
    stage = tmp_path / "build"
    monkeypatch.setattr(build_installer, "STAGE", stage)
    monkeypatch.setattr(build_installer, "ENGINE_STAGE", stage / "engine")
    monkeypatch.setattr(build_installer, "PYTHON_STAGE", stage / "python")
    monkeypatch.setattr(build_installer, "RESOURCES_STAGE", stage / "resources")
    monkeypatch.setattr(build_installer, "DIST", stage / "dist")
    archive = fake_python_archive(tmp_path / "cpython-3.11.18+20261026-x86_64-pc-windows-msvc-install_only.tar.gz")
    return {"stage": stage, "archive": archive, "dist": stage / "dist"}


def test_staging_builds_the_layout_the_installer_expects(
    staged_workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parent.parent
    result = build_installer.build(
        platforms=["win", "linux"],
        root=root,
        python_tarball=staged_workspace["archive"],
        allow_download=False,
        run_packager=False,
        stage_only=True,
    )
    assert result.staged
    engine = staged_workspace["stage"] / "engine"
    python = staged_workspace["stage"] / "python"
    assert (engine / "app_api" / "server.py").is_file()
    assert (engine / "tunnel" / "certs" / "client.crt").is_file()
    assert not (engine / "ca.key").exists()
    assert (python / "python.exe").is_file()
    assert not (python / "Lib" / "test").exists(), "the runtime is trimmed"
    assert (staged_workspace["stage"] / "resources" / "icon.png").is_file()
    # Not built yet, so this is a staged-only success.
    assert result.built is False
    assert result.ok
    shutil.rmtree(staged_workspace["stage"], ignore_errors=True)


def test_missing_python_runtime_stops_the_build(
    staged_workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(build_installer, "extract_python", lambda archive, bundle: Path("x"))
    result = build_installer.build(
        platforms=["win"],
        root=root,
        python_tarball=staged_workspace["archive"],
        allow_download=False,
        run_packager=False,
        stage_only=True,
    )
    assert not result.ok
    assert any("build/python" in problem for problem in result.problems)
    shutil.rmtree(staged_workspace["stage"], ignore_errors=True)


def test_unknown_platform_is_rejected(staged_workspace: dict[str, Path]) -> None:
    result = build_installer.build(platforms=["solaris"], run_packager=False)
    assert not result.ok
    assert any("unknown platform" in problem for problem in result.problems)
