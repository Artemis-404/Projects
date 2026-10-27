"""Build the portable Windows artifact: a folder containing ``UQAB.exe``.

The artifact is what FR-31 asks for: unzip, double-click, no installer, no admin
rights. It contains three things:

1. the Electron runtime, with ``electron.exe`` renamed to ``UQAB.exe``;
2. our Electron app (``resources/app``) - the Min-derived browser shell;
3. a self-contained CPython plus the whole UQAB engine (``resources/app/engine``).

Why a folder and not an installer
---------------------------------
Electron's installer targets (NSIS, portable stub) need Wine to build on Linux and
add a packing layer that only exists to satisfy an installer wizard. Renaming the
Electron binary produces a genuine ``.exe`` that starts the app directly, and it
can be zipped, copied to a USB stick and run from any path.

Downloads
---------
Two archives are fetched from GitHub releases and cached in ``build/cache``:

* ``electron-v<version>-win32-x64.zip`` from ``electron/electron``;
* ``cpython-3.11.*-x86_64-pc-windows-msvc-install_only.tar.gz`` from
  ``astral-sh/python-build-standalone`` (the same runtime Astral ships for uv).

Both can be supplied from disk with ``--electron-zip`` / ``--python-archive`` so a
build can be repeated offline, and ``--no-download`` assembles the bundle with
whatever is already cached.

Usage::

    python packaging/build_portable.py                 # build + verify + zip
    python packaging/build_portable.py --check-only    # verify an existing bundle
    python packaging/build_portable.py --keep-build    # leave the tree unzipped
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
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from packaging.manifest import (
    APP_ITEMS,
    ENGINE_DATA,
    ENGINE_ITEMS,
    EXCLUDE_DIRS,
    README,
    iter_source_files,
    verify_layout,
)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ELECTRON = "43.4.1"
DEFAULT_PYTHON_MAJOR_MINOR = "3.11"
GITHUB = "https://github.com"
PYTHON_RELEASES = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"


@dataclass(slots=True)
class BuildResult:
    bundle: Path
    archive: Path | None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, object]:
        return {
            "bundle": str(self.bundle),
            "archive": str(self.archive) if self.archive else "",
            "ok": self.ok,
            "problems": self.problems,
            "notes": self.notes,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def download(url: str, target: Path, *, timeout: float = 600.0) -> Path:
    """Download ``url`` to ``target`` with a progress hint; returns the path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        log(f"using cached {target.name} ({target.stat().st_size // 1024 // 1024} MiB)")
        return target
    log(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "uqab-build/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        seen = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            seen += len(chunk)
            if total and seen % (20 << 20) < (1 << 20):
                log(f"  {seen // 1024 // 1024} / {total // 1024 // 1024} MiB")
    log(f"downloaded {target.name} ({target.stat().st_size // 1024 // 1024} MiB)")
    return target


def electron_zip(version: str, cache: Path, *, allow_download: bool = True) -> Path:
    target = cache / f"electron-v{version}-win32-x64.zip"
    if target.is_file():
        return target
    if not allow_download:
        raise FileNotFoundError(f"{target} is not cached and downloads are disabled")
    url = f"{GITHUB}/electron/electron/releases/download/v{version}/electron-v{version}-win32-x64.zip"
    return download(url, target)


# python-build-standalone target triple per installer platform.
PYTHON_TRIPLES = {"win": "x86_64-pc-windows-msvc", "linux": "x86_64-unknown-linux-gnu"}


def python_archive(
    cache: Path,
    *,
    major_minor: str = DEFAULT_PYTHON_MAJOR_MINOR,
    allow_download: bool = True,
    platform: str = "win",
) -> Path:
    triple = PYTHON_TRIPLES[platform]
    cached = sorted(cache.glob(f"cpython-{major_minor}.*-{triple}-install_only.tar.gz"))
    if cached:
        return cached[-1]
    if not allow_download:
        raise FileNotFoundError("no CPython archive in the cache and downloads are disabled")
    log("resolving the latest python-build-standalone release")
    request = urllib.request.Request(PYTHON_RELEASES, headers={"User-Agent": "uqab-build/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    pattern = re.compile(
        rf"^cpython-{re.escape(major_minor)}\.\d+\+.*-{re.escape(triple)}-install_only\.tar\.gz$"
    )
    names = [asset["name"] for asset in payload.get("assets", []) if pattern.match(asset["name"])]
    if not names:
        raise RuntimeError(
            f"no CPython {major_minor} {triple} build in release {payload.get('tag_name')}"
        )
    name = sorted(names)[-1]
    url = f"{GITHUB}/astral-sh/python-build-standalone/releases/download/{payload['tag_name']}/{name}"
    return download(url, cache / name)


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def copy_engine(root: Path, engine_dir: Path) -> list[str]:
    """Copy every engine package and data file; returns the copied top-level names."""
    copied: list[str] = []
    engine_dir.mkdir(parents=True, exist_ok=True)
    for item in ENGINE_ITEMS + ENGINE_DATA:
        files = iter_source_files(root, item)
        for file in files:
            _copy(file, engine_dir / file.relative_to(root))
        if files:
            copied.append(item)
    # The lab certificates are generated, not committed: build them now.
    _ensure_certs(root, engine_dir)
    return copied


def _ensure_certs(root: Path, engine_dir: Path) -> None:
    """Generate the mutual-TLS certificates inside the bundle (openssl needed)."""
    target = engine_dir / "tunnel" / "certs"
    needed = ["ca.crt", "client.crt", "client.key", "server.crt", "server.key"]
    if all((target / name).is_file() for name in needed):
        return
    source = root / "tunnel" / "certs"
    if all((source / name).is_file() for name in needed):
        log("copying the repository's lab certificates")
        target.mkdir(parents=True, exist_ok=True)
        # The CA *private* key stays out of the bundle: verification needs ca.crt,
        # and the leaf key/cert pair is what the tunnel presents.
        for name in needed:
            if (source / name).is_file():
                _copy(source / name, target / name)
        return
    if not shutil.which("openssl"):
        raise RuntimeError(
            "openssl is required to create the lab certificates for the bundle; "
            "install it, or place generated certificates in tunnel/certs first"
        )
    log("generating lab certificates with openssl")
    subprocess.run(
        [sys.executable, "-m", "tunnel.certs", "--out", str(target)],
        cwd=str(root),
        check=True,
    )


def copy_app(root: Path, app_dir: Path) -> list[str]:
    """Copy the Electron app (without node_modules or tests)."""
    copied: list[str] = []
    for item in APP_ITEMS:
        source = root / "app" / item
        if not source.exists():
            raise FileNotFoundError(f"app item missing: {source}")
        if source.is_dir():
            for file in sorted(source.rglob("*")):
                if not file.is_file():
                    continue
                relative = file.relative_to(source)
                if any(part in EXCLUDE_DIRS for part in relative.parts):
                    continue
                _copy(file, app_dir / item / relative)
            copied.append(item)
        else:
            _copy(source, app_dir / item)
            copied.append(item)
    return copied


def extract_electron(archive: Path, bundle: Path) -> Path:
    """Unpack the Electron runtime and rename the launcher to UQAB.exe."""
    bundle.mkdir(parents=True, exist_ok=True)
    log(f"extracting {archive.name}")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(bundle)
    exe = bundle / "electron.exe"
    if not exe.is_file():
        raise RuntimeError("the Electron archive did not contain electron.exe")
    target = bundle / "UQAB.exe"
    if target.exists():
        target.unlink()
    exe.rename(target)
    default_app = bundle / "resources" / "default_app.asar"
    if default_app.exists():
        default_app.unlink()
    version_file = bundle / "version"
    if version_file.exists():
        (bundle / "electron-version.txt").write_text(
            version_file.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
        )
    return target


def python_executable(runtime: Path, platform: str = "win") -> Path:
    """Where the interpreter lives in an install_only runtime."""
    return runtime / "python.exe" if platform == "win" else runtime / "bin" / "python3"


def extract_python(archive: Path, bundle: Path, platform: str = "win") -> Path:
    """Unpack CPython into ``resources/python`` and make it self-contained."""
    target = bundle / "resources" / "python"
    if target.exists():
        shutil.rmtree(target)
    log(f"extracting {archive.name}")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(target, filter="data")  # Python >= 3.12 rejects unsafe paths
        except TypeError:  # pragma: no cover - Python < 3.11.4
            tf.extractall(target)
    # python-build-standalone's install_only layout is python/{python.exe,...}
    inner = target / "python"
    if inner.is_dir():
        for item in inner.iterdir():
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()
    exe = python_executable(target, platform)
    if not exe.is_file():
        raise RuntimeError(
            f"the CPython archive did not contain {exe.relative_to(target).as_posix()}"
        )
    # Trim what a GUI app never needs, to keep the artifact small. The standard
    # library is Lib/ on Windows and lib/pythonX.Y/ on Linux.
    stdlib_dirs = [target / "Lib", *sorted((target / "lib").glob("python3.*"))]
    for stdlib in stdlib_dirs:
        for unwanted in ("test", "tests", "Doc", "doc", "lib2to3", "idlelib", "tkinter", "turtledemo"):
            candidate = stdlib / unwanted
            if candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
    for pycache in target.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    return exe


def assemble(
    root: Path = REPO,
    *,
    out_dir: Path | None = None,
    electron_version: str = DEFAULT_ELECTRON,
    electron_archive: Path | None = None,
    python_tarball: Path | None = None,
    allow_download: bool = True,
    cache: Path | None = None,
) -> BuildResult:
    """Assemble the portable bundle and verify it."""
    started = time.time()
    cache = cache or (root / "build" / "cache")
    bundle = out_dir or (root / "build" / "UQAB")
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    ezip = electron_archive or electron_zip(electron_version, cache, allow_download=allow_download)
    pzip = python_tarball or python_archive(cache, allow_download=allow_download)

    extract_electron(ezip, bundle)
    extract_python(pzip, bundle)

    app_dir = bundle / "resources" / "app"
    app_copied = copy_app(root, app_dir)
    engine_copied = copy_engine(root, app_dir / "engine")
    log(f"app items: {len(app_copied)}; engine items: {len(engine_copied)}")

    _copy(root / "README.md", app_dir / "README.md")
    (bundle / "LICENSE").write_text((root / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8")
    (bundle / "README.txt").write_text(README, encoding="utf-8")
    (bundle / "build-info.json").write_text(
        json.dumps(
            {
                "product": "UQAB",
                "electron": electron_version,
                "python": pzip.name,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "git": _git_commit(root),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not (root / "LICENSE").is_file():
        notes.append("the repository has no LICENSE file yet; one was generated for the bundle")

    problems = verify_layout(bundle)
    return BuildResult(
        bundle=bundle,
        archive=None,
        problems=problems,
        notes=notes,
        elapsed_s=time.time() - started,
    )


def _git_commit(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def zip_bundle(bundle: Path) -> Path:
    """Zip the bundle as ``UQAB-portable-win64.zip`` next to the folder."""
    archive = bundle.parent / "UQAB-portable-win64.zip"
    log(f"zipping {bundle.name} -> {archive.name}")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file in sorted(bundle.rglob("*")):
            if not file.is_file():
                continue
            relative = (Path("UQAB") / file.relative_to(bundle)).as_posix()
            info = zipfile.ZipInfo(relative, date_time=_safe_time(file))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with file.open("rb") as handle:
                zf.writestr(info, handle.read())
    return archive


def _safe_time(path: Path) -> tuple[int, int, int, int, int, int]:
    """A zip-legal timestamp; files extracted from tarballs can carry mtime 0."""
    try:
        stamp = time.gmtime(path.stat().st_mtime)
    except OSError:  # pragma: no cover - the file just vanished
        stamp = time.gmtime()
    if stamp.tm_year < 1980:
        stamp = time.gmtime()
    return (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, stamp.tm_sec)


def engine_smoke_test(bundle: Path, *, python: str = sys.executable) -> list[str]:
    """Run the bundled engine with *this* interpreter to prove the copy is complete.

    The Windows interpreter cannot execute here, but the engine tree in the bundle
    can: if the copy is complete, the API and the import graph work unchanged.
    """
    engine = bundle / "resources" / "app" / "engine"
    problems: list[str] = []
    if not engine.is_dir():
        return ["the bundle has no engine directory"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(engine)
    code = (
        "import app_api.server, app_api.engine, pt.client, pt.server, lab.stack, "
        "experiments.compare, dashboard.server, analysis.classifier, profile.learner, "
        "persona.llm, storage.db; print('engine ok')"
    )
    result = subprocess.run(
        [python, "-c", code],
        cwd=str(engine),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if "engine ok" not in result.stdout:
        problems.append(f"engine import smoke test failed: {result.stderr.strip()[:400]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the portable UQAB bundle for Windows")
    parser.add_argument("--out", default=None, help="bundle directory (default build/UQAB)")
    parser.add_argument("--electron", default=DEFAULT_ELECTRON, help="Electron version")
    parser.add_argument("--electron-zip", default=None, help="use this Electron zip")
    parser.add_argument("--python-archive", default=None, help="use this CPython tar.gz")
    parser.add_argument("--cache", default=None, help="download cache (default build/cache)")
    parser.add_argument("--no-download", action="store_true", help="never touch the network")
    parser.add_argument("--no-zip", action="store_true", help="leave the bundle unpacked")
    parser.add_argument("--keep-build", action="store_true", help="same as --no-zip")
    parser.add_argument("--check-only", action="store_true", help="verify an existing bundle")
    parser.add_argument("--no-smoke", action="store_true", help="skip the engine smoke test")
    args = parser.parse_args(argv)

    root = REPO
    bundle = Path(args.out) if args.out else root / "build" / "UQAB"

    if args.check_only:
        problems = verify_layout(bundle)
        print(json.dumps({"bundle": str(bundle), "problems": problems}, indent=2))
        return 0 if not problems else 1

    result = assemble(
        root,
        out_dir=bundle,
        electron_version=args.electron,
        electron_archive=Path(args.electron_zip) if args.electron_zip else None,
        python_tarball=Path(args.python_archive) if args.python_archive else None,
        allow_download=not args.no_download,
        cache=Path(args.cache) if args.cache else None,
    )
    if not args.no_smoke:
        result.problems.extend(engine_smoke_test(result.bundle))
    if not result.ok:
        print(json.dumps(result.to_dict(), indent=2))
        log("build failed")
        return 1
    if not (args.no_zip or args.keep_build):
        result.archive = zip_bundle(result.bundle)
    print(json.dumps(result.to_dict(), indent=2))
    log(f"done in {result.elapsed_s:.1f}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
