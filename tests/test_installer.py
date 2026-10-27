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


def test_builder_resolves_npx_cmd_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    shim = r"C:\Program Files\nodejs\npx.CMD"
    monkeypatch.setattr(build_installer.shutil, "which", lambda name: shim if name == "npx" else None)
    command = build_installer.resolve_executable(["npx", "--yes", "electron-builder@26"])
    assert command == [shim, "--yes", "electron-builder@26"]
    monkeypatch.setattr(build_installer.shutil, "which", lambda name: None)
    assert build_installer.resolve_executable(["npx", "-v"]) == ["npx", "-v"]


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
    monkeypatch.setattr(
        build_installer, "extract_python", lambda archive, bundle, platform="win": Path("x")
    )
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


def fake_linux_python_archive(path: Path) -> Path:
    """python-build-standalone's Linux install_only layout."""
    with tarfile.open(path, "w:gz") as tf:
        for name, payload, mode in (
            ("python/bin/python3", b"\x7fELF interpreter", 0o755),
            ("python/lib/python3.11/os.py", b"# stdlib", 0o644),
            ("python/lib/python3.11/test/test_os.py", b"trimmed", 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            tf.addfile(info, io.BytesIO(payload))
    return path


def test_linux_build_stages_the_linux_runtime(
    staged_workspace: dict[str, Path], tmp_path: Path
) -> None:
    """--linux must bundle bin/python3, not the Windows python.exe."""
    root = Path(__file__).resolve().parent.parent
    archive = fake_linux_python_archive(
        tmp_path / "cpython-3.11.18+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz"
    )
    result = build_installer.build(
        platforms=["linux"],
        root=root,
        python_tarball=archive,
        allow_download=False,
        run_packager=False,
        stage_only=True,
    )
    assert result.ok, result.problems
    python = staged_workspace["stage"] / "python"
    assert (python / "bin" / "python3").is_file()
    assert not (python / "python.exe").exists()
    assert not (python / "lib" / "python3.11" / "test").exists(), "the runtime is trimmed"
    # A Windows archive is rejected for a Linux build: no interpreter where Linux looks.
    windows = build_installer.build(
        platforms=["linux"],
        root=root,
        python_tarball=staged_workspace["archive"],
        allow_download=False,
        run_packager=False,
        stage_only=True,
    )
    assert not windows.ok
    shutil.rmtree(staged_workspace["stage"], ignore_errors=True)


def test_python_archive_picks_the_platform_triple(tmp_path: Path) -> None:
    from packaging.build_portable import python_archive

    win = tmp_path / "cpython-3.11.18+20261026-x86_64-pc-windows-msvc-install_only.tar.gz"
    linux = tmp_path / "cpython-3.11.18+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz"
    for path in (win, linux):
        path.write_bytes(b"x")
    assert python_archive(tmp_path, allow_download=False, platform="win") == win
    assert python_archive(tmp_path, allow_download=False, platform="linux") == linux
