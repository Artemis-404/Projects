"""Build installable packages: a Windows ``.exe`` setup and Linux AppImage/.deb.

This is the "publishable app" path. Unlike ``build_portable.py`` (a folder you
unzip), this produces installers that put UQAB in the Start menu / application
menu, register an uninstaller and ship everything the user needs - including the
Python runtime, so nobody has to install Python or Tor to try the app.

What it does
------------
1. **Stages the engine** into ``build/engine`` (same manifest as the portable
   build: packages, personas, config, docs, generated lab certificates).
2. **Stages the Python runtime** into ``build/python`` (python-build-standalone,
   downloaded once and cached in ``build/cache``). The app finds it as
   ``resources/python`` beside ``app.asar``.
3. **Runs electron-builder** with ``packaging/electron-builder.json``:
   * Windows: NSIS setup, per-user install (no administrator prompt), optional
     install directory, desktop + Start-menu shortcuts;
   * Linux: AppImage (single file, double-click to run) and ``.deb`` (application
     menu entry, ``apt install ./UQAB-*.deb``).
4. **Verifies** the artifacts and writes ``build/dist/build-report.json``.

Requirements on the build machine: Node.js (for electron-builder), network access
for the Electron download, and ``openssl`` if the lab certificates do not exist
yet. Everything else is in this repository.

Usage::

    python packaging/build_installer.py                 # both platforms
    python packaging/build_installer.py --win           # Windows setup only
    python packaging/build_installer.py --linux         # AppImage + deb
    python packaging/build_installer.py --check         # validate the config only
    python packaging/build_installer.py --no-run        # stage everything, don't pack
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Allow `python packaging/<script>.py` as well as `python -m packaging.<script>`:
# without this, "packaging" would resolve to the PyPI package of the same name.
if __package__ in (None, ""):
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from packaging.build_portable import (
    REPO,
    _copy,
    copy_engine,
    extract_python,
    python_archive,
    python_executable,
)
from packaging.manifest import verify_layout

CONFIG = REPO / "packaging" / "electron-builder.json"
STAGE = REPO / "build"
ENGINE_STAGE = STAGE / "engine"
PYTHON_STAGE = STAGE / "python"
RESOURCES_STAGE = STAGE / "resources"
DIST = STAGE / "dist"


@dataclass(slots=True)
class InstallerResult:
    staged: bool = False
    built: bool = False
    artifacts: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems and (self.built or self.staged)

    def to_dict(self) -> dict[str, object]:
        return {
            "staged": self.staged,
            "built": self.built,
            "artifacts": [str(path) for path in self.artifacts],
            "problems": self.problems,
            "notes": self.notes,
            "elapsed_s": round(self.elapsed_s, 1),
        }


def check_config(path: Path = CONFIG) -> list[str]:
    """Validate the electron-builder configuration without running it."""
    problems: list[str] = []
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cannot read {path}: {exc}"]
    for key in ("appId", "productName", "directories", "files", "extraResources"):
        if key not in config:
            problems.append(f"config is missing {key!r}")
    app_dir = (path.parent / config["directories"].get("app", ".")).resolve()
    if not (app_dir / "main.js").is_file():
        problems.append(f"'directories.app' does not contain main.js: {app_dir}")
    if not (app_dir / "assets" / "icon.ico").is_file():
        problems.append("the Windows icon is missing: app/assets/icon.ico")
    for resource in config.get("extraResources", []):
        source = (path.parent / resource["from"]).resolve()
        # Sources under build/ are produced by the staging step that runs before
        # electron-builder, so they are allowed to be absent at check time - but
        # anything else must exist, otherwise every build would ship a broken app.
        staged = "build" in source.parts
        if not source.exists() and not staged:
            problems.append(f"extraResources source does not exist: {source}")
        if not staged and resource["to"] in {"engine", "python"}:
            problems.append(
                f"extraResources {resource['to']!r} must come from build/, not {source}"
            )
    win = config.get("win", {})
    targets = [entry["target"] if isinstance(entry, dict) else entry for entry in win.get("target", [])]
    if "nsis" not in targets:
        problems.append("win.target must include 'nsis' so Windows users get a .exe setup")
    nsis = config.get("nsis", {})
    if nsis.get("perMachine") is not False:
        problems.append("nsis.perMachine must be false: the installer must not need admin rights")
    if nsis.get("oneClick") is not False:
        problems.append("nsis.oneClick must be false so the user can choose a folder")
    linux = config.get("linux", {})
    linux_targets = [
        entry["target"] if isinstance(entry, dict) else entry
        for entry in linux.get("target", [])
    ]
    for wanted in ("AppImage", "deb"):
        if wanted not in linux_targets:
            problems.append(f"linux.target must include {wanted!r}")
    if linux.get("category") != "Network":
        problems.append("linux.category should be Network")
    return problems


def stage_engine(root: Path = REPO) -> list[str]:
    """Copy the engine and the Python runtime into the builder's staging areas."""
    if ENGINE_STAGE.exists():
        shutil.rmtree(ENGINE_STAGE)
    copy_engine(root, ENGINE_STAGE)
    RESOURCES_STAGE.mkdir(parents=True, exist_ok=True)
    _copy(root / "build" / "icon.png" if (root / "build" / "icon.png").is_file()
          else root / "app" / "assets" / "icon.png", RESOURCES_STAGE / "icon.png")
    return ["engine", "resources/icon.png"]


def stage_python(
    root: Path = REPO,
    *,
    archive: Path | None = None,
    allow_download: bool = True,
    platform: str = "win",
) -> list[str]:
    """Place the target platform's CPython at ``build/python`` for ``extraResources``."""
    if PYTHON_STAGE.exists():
        shutil.rmtree(PYTHON_STAGE)
    tarball = archive or python_archive(
        root / "build" / "cache", allow_download=allow_download, platform=platform
    )
    fake_bundle = STAGE / "python-stage"
    if fake_bundle.exists():
        shutil.rmtree(fake_bundle)
    fake_bundle.mkdir(parents=True, exist_ok=True)
    extract_python(tarball, fake_bundle, platform)
    extracted = fake_bundle / "resources" / "python"
    if not extracted.is_dir():
        shutil.rmtree(fake_bundle, ignore_errors=True)
        raise RuntimeError(
            f"{tarball.name} produced no runtime, so build/python has no interpreter"
        )
    shutil.move(str(extracted), str(PYTHON_STAGE))
    shutil.rmtree(fake_bundle, ignore_errors=True)
    return [str(PYTHON_STAGE)]


def verify_stage(platform: str = "win") -> list[str]:
    problems: list[str] = []
    if not (ENGINE_STAGE / "app_api" / "server.py").is_file():
        problems.append("build/engine is missing app_api/server.py")
    for package in ("pt", "tunnel", "cover", "protocol", "persona", "profile", "dashboard"):
        if not (ENGINE_STAGE / package).is_dir():
            problems.append(f"build/engine is missing {package}/")
    if not (ENGINE_STAGE / "tunnel" / "certs" / "client.crt").is_file():
        problems.append("build/engine is missing the lab certificates")
    if not python_executable(PYTHON_STAGE, platform).is_file():
        problems.append(f"build/python does not contain the {platform} interpreter")
    if not (RESOURCES_STAGE / "icon.png").is_file():
        problems.append("build/resources/icon.png is missing")
    problems.extend(verify_layout(REPO / "build" / "UQAB") if (REPO / "build" / "UQAB").is_dir() else [])
    return problems


def electron_builder_command(platforms: list[str]) -> list[str]:
    """The electron-builder invocation for the requested platforms."""
    command = [
        "npx",
        "--yes",
        "electron-builder@26",
        "--config",
        str(CONFIG),
        "--projectDir",
        str(REPO / "app"),
    ]
    for platform in platforms:
        command += ["--" + platform]
    return command


def resolve_executable(command: list[str]) -> list[str]:
    """Resolve argv[0] on PATH: on Windows `npx` is `npx.cmd`, which
    subprocess cannot find by its bare name."""
    found = shutil.which(command[0])
    return [found, *command[1:]] if found else command


def run_builder(platforms: list[str], *, timeout: float = 3600.0) -> tuple[bool, str]:
    command = resolve_executable(electron_builder_command(platforms))
    print("[installer] " + " ".join(command), flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, "npx was not found: install Node.js to build the installers"
    except subprocess.TimeoutExpired:
        return False, "electron-builder timed out"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output


def collect_artifacts(dist: Path = DIST) -> list[Path]:
    wanted = (".exe", ".AppImage", ".deb", ".yml", ".blockmap")
    if not dist.is_dir():
        return []
    return sorted(
        path for path in dist.iterdir() if path.is_file() and path.suffix in wanted
    )


def build(
    *,
    platforms: list[str] | None = None,
    root: Path = REPO,
    python_tarball: Path | None = None,
    allow_download: bool = True,
    run_packager: bool = True,
    stage_only: bool = False,
) -> InstallerResult:
    started = time.time()
    result = InstallerResult()
    platforms = platforms or ["win", "linux"]
    for platform in platforms:
        if platform not in {"win", "linux"}:
            result.problems.append(f"unknown platform {platform!r}")
    result.problems.extend(check_config(CONFIG))
    if result.problems:
        return result

    try:
        stage_engine(root)
    except (OSError, RuntimeError) as exc:
        result.problems.append(f"staging failed: {exc}")
        return result

    # One platform at a time: build/python holds the runtime for exactly one
    # target (python.exe for Windows, bin/python3 for Linux).
    for platform in platforms:
        try:
            stage_python(
                root, archive=python_tarball, allow_download=allow_download, platform=platform
            )
        except (OSError, RuntimeError) as exc:
            result.problems.append(f"staging failed: {exc}")
            return result
        result.staged = True
        result.problems.extend(verify_stage(platform))
        if result.problems or stage_only or not run_packager:
            result.elapsed_s = time.time() - started
            if not run_packager and not result.problems:
                result.notes.append("staged only: run without --no-run to build the installers")
            return result

        ok, output = run_builder([platform])
        result.built = ok
        if not ok:
            tail = "\n".join(output.strip().splitlines()[-15:])
            result.problems.append(f"electron-builder failed ({platform}):\n{tail}")
            result.elapsed_s = time.time() - started
            return result

    result.artifacts = collect_artifacts()
    if not result.artifacts:
        result.problems.append("electron-builder reported success but produced no artifacts")
    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "build-report.json").write_text(
        json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    result.notes.append(
        "Windows: run UQAB-Setup-<version>.exe (installs per-user, no admin prompt)."
    )
    result.notes.append(
        "Linux: chmod +x UQAB-*.AppImage and run it, or sudo apt install ./UQAB-*.deb."
    )
    result.elapsed_s = time.time() - started
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the UQAB installers")
    parser.add_argument("--win", action="store_true", help="build the Windows setup only")
    parser.add_argument("--linux", action="store_true", help="build the Linux packages only")
    parser.add_argument("--check", action="store_true", help="validate the configuration only")
    parser.add_argument("--no-run", action="store_true", help="stage files, do not run electron-builder")
    parser.add_argument("--python-archive", default=None, help="use this CPython tar.gz")
    parser.add_argument("--no-download", action="store_true", help="never touch the network")
    args = parser.parse_args(argv)

    if args.check:
        problems = check_config()
        print(json.dumps({"config": str(CONFIG), "problems": problems}, indent=2))
        return 0 if not problems else 1

    platforms: list[str] = []
    if args.win:
        platforms.append("win")
    if args.linux:
        platforms.append("linux")
    if not platforms:
        platforms = ["win", "linux"]

    result = build(
        platforms=platforms,
        python_tarball=Path(args.python_archive) if args.python_archive else None,
        allow_download=not args.no_download,
        run_packager=not args.no_run,
        stage_only=args.no_run,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
