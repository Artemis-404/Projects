"""Packaging tests: what goes into the portable bundle, and a staged dry build (FR-31).

The real build downloads Electron (~100 MB) and a Windows CPython (~20 MB), which
the test suite must not depend on. These tests therefore check the manifest, the
verifier's failure modes and a *staged* assembly: every copy step runs for real
against a fake Electron zip and a fake CPython archive.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from packaging import build_portable, manifest


def test_manifest_excludes_tests_and_caches() -> None:
    assert manifest.is_excluded(Path("tests/test_api.py"))
    assert manifest.is_excluded(Path("app/__pycache__/main.cpython-311.pyc"))
    assert manifest.is_excluded(Path("data/uqab.sqlite"))
    assert manifest.is_excluded(Path("run/logs/slots.jsonl.pcap"))
    assert not manifest.is_excluded(Path("app/main.js"))
    assert not manifest.is_excluded(Path("config/config.yaml"))


def test_engine_items_exist_in_the_repository() -> None:
    root = Path(__file__).resolve().parent.parent
    for item in manifest.ENGINE_ITEMS + manifest.ENGINE_DATA:
        assert (root / item).exists(), f"manifest lists a missing item: {item}"
    for item in manifest.APP_ITEMS:
        assert (root / "app" / item).exists(), f"manifest lists a missing app item: {item}"


def test_private_keys_are_not_shipped() -> None:
    root = Path(__file__).resolve().parent.parent
    files = manifest.iter_source_files(root, "tunnel")
    names = {path.name for path in files}
    assert "ca.key" not in names
    assert "client.key" not in names
    assert "server.key" not in names


def test_verifier_reports_missing_pieces(tmp_path: Path) -> None:
    problems = manifest.verify_layout(tmp_path)
    assert any("UQAB.exe" in problem for problem in problems)
    assert any("engine package missing" in problem for problem in problems)


def test_verifier_accepts_a_complete_stub(tmp_path: Path) -> None:
    for name in manifest.REQUIRED_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("config:\n  paths: {}\n" if name.endswith(".yaml") else "x", encoding="utf-8")
    engine = tmp_path / "resources" / "app" / "engine"
    for package in ("app_api", "pt", "tunnel", "cover", "protocol", "persona", "profile"):
        (engine / package).mkdir(parents=True, exist_ok=True)
    (tmp_path / "resources" / "python").mkdir(parents=True, exist_ok=True)
    (tmp_path / manifest.PYTHON_EXE).write_text("MZ", encoding="utf-8")
    assert manifest.verify_layout(tmp_path) == []


def test_verifier_flags_private_material(tmp_path: Path) -> None:
    for name in manifest.REQUIRED_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("paths:\n" if name.endswith(".yaml") else "x", encoding="utf-8")
    engine = tmp_path / "resources" / "app" / "engine"
    for package in ("app_api", "pt", "tunnel", "cover", "protocol", "persona", "profile"):
        (engine / package).mkdir(parents=True, exist_ok=True)
    (engine / "ca.key").write_text("private", encoding="utf-8")
    (engine / "__pycache__").mkdir()
    (engine / "tests").mkdir()
    problems = manifest.verify_layout(tmp_path)
    assert any("CA private key" in problem for problem in problems)
    assert any("__pycache__" in problem for problem in problems)
    assert any("test suite" in problem for problem in problems)


# ------------------------------------------------------------------ staged build


def fake_electron_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("electron.exe", b"MZ fake launcher")
        zf.writestr("resources/default_app.asar", b"asar")
        zf.writestr("version", "43.4.1")
        zf.writestr("chrome_100_percent.pak", b"pak")
        zf.writestr("locales/en-US.pak", b"pak")
    return path


def fake_python_archive(path: Path) -> Path:
    """A CPython-shaped tar.gz: python/python.exe + Lib/site-packages + junk."""
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in (
            ("python/python.exe", b"MZ fake interpreter"),
            ("python/LICENSE.txt", b"PSF"),
            ("python/Lib/os.py", b"# stdlib"),
            ("python/Lib/site-packages/README.txt", b"x"),
            ("python/Lib/test/test_os.py", b"# tests are trimmed"),
            ("python/Lib/tkinter/__init__.py", b"# tk is trimmed"),
            ("python/Lib/__pycache__/os.cpython-311.pyc", b"\x00"),
        ):
            data = payload
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture()
def staged(tmp_path: Path) -> dict[str, Path]:
    """Fake downloadable archives plus an isolated output directory."""
    cache = tmp_path / "cache"
    cache.mkdir()
    return {
        "electron": fake_electron_zip(cache / "electron-v43.4.1-win32-x64.zip"),
        "python": fake_python_archive(
            cache / "cpython-3.11.18+20261026-x86_64-pc-windows-msvc-install_only.tar.gz"
        ),
        "out": tmp_path / "build" / "UQAB",
    }


def test_staged_assembly_produces_a_verifiable_bundle(staged: dict[str, Path]) -> None:
    root = Path(__file__).resolve().parent.parent
    result = build_portable.assemble(
        root,
        out_dir=staged["out"],
        electron_archive=staged["electron"],
        python_tarball=staged["python"],
        allow_download=False,
        cache=staged["electron"].parent,
    )
    assert result.bundle == staged["out"]
    assert (result.bundle / "UQAB.exe").is_file()
    assert not (result.bundle / "electron.exe").exists()
    assert (result.bundle / "resources" / "python" / "python.exe").is_file()
    assert not (result.bundle / "resources" / "python" / "Lib" / "test").exists()
    assert not (result.bundle / "resources" / "python" / "Lib" / "tkinter").exists()
    assert not list((result.bundle / "resources" / "python").rglob("__pycache__"))
    assert (result.bundle / "README.txt").is_file()
    assert (result.bundle / "build-info.json").is_file()
    info = json.loads((result.bundle / "build-info.json").read_text(encoding="utf-8"))
    assert info["product"] == "UQAB"
    assert "electron" in info

    engine = result.bundle / "resources" / "app" / "engine"
    assert (engine / "app_api" / "server.py").is_file()
    assert (engine / "persona" / "profiles" / "balanced.json").is_file()
    assert (engine / "tunnel" / "certs" / "client.crt").is_file()
    assert not (engine / "ca.key").exists()
    assert not (engine / "tests").exists()

    app = result.bundle / "resources" / "app"
    assert (app / "main.js").is_file()
    assert (app / "renderer" / "index.html").is_file()
    assert (app / "NOTICE").is_file()
    assert not (app / "test").exists()
    assert not (app / "node_modules").exists()

    problems = manifest.verify_layout(result.bundle)
    assert problems == [], problems
    assert result.problems == []

    failures = build_portable.engine_smoke_test(result.bundle)
    assert failures == [], failures


def test_assembly_refuses_to_download_when_disabled(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    with pytest.raises(FileNotFoundError):
        build_portable.assemble(
            root,
            out_dir=tmp_path / "out",
            allow_download=False,
            cache=tmp_path / "empty-cache",
        )


def test_zip_bundle_contains_the_launcher(staged: dict[str, Path]) -> None:
    root = Path(__file__).resolve().parent.parent
    result = build_portable.assemble(
        root,
        out_dir=staged["out"],
        electron_archive=staged["electron"],
        python_tarball=staged["python"],
        allow_download=False,
        cache=staged["electron"].parent,
    )
    archive = build_portable.zip_bundle(result.bundle)
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "UQAB/UQAB.exe" in names
    assert "UQAB/resources/app/engine/app_api/server.py" in names
    assert "UQAB/README.txt" in names
    shutil.rmtree(result.bundle.parent, ignore_errors=True)
