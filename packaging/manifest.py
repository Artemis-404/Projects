"""The portable Windows layout, as data (FR-31, SDD 8).

One module owns the answer to "what exactly goes into UQAB.exe?", so the builder,
the verifier, the tests and the documentation cannot drift apart.

Layout produced by ``packaging/build_portable.py``::

    UQAB/
      UQAB.exe                     renamed Electron binary (no installer, no admin)
      LICENSE                      UQAB licence
      README.txt                   how to run, what lab mode means
      resources/
        app/                       our Electron app (main.js, renderer/, assets/)
          engine/                  the whole Python engine + config + personas + docs
        python/                    self-contained CPython (python.exe + stdlib)
        *.pak, *.dll, ...          Electron runtime files
"""

from __future__ import annotations

from pathlib import Path

#: Python packages/modules copied into ``resources/app/engine``.
ENGINE_ITEMS: tuple[str, ...] = (
    "analysis",
    "app_api",
    "config",
    "cover",
    "dashboard",
    "experiments",
    "lab",
    "persona",
    "profile",
    "protocol",
    "pt",
    "security",
    "storage",
    "tunnel",
    "logs.py",
)

#: Non-Python data the engine needs at run time.
ENGINE_DATA: tuple[str, ...] = (
    "config/config.yaml",
    "persona/profiles",
    "persona/schema.json",
    "docs",
)

#: The Electron app files copied to ``resources/app``.
APP_ITEMS: tuple[str, ...] = (
    "main.js",
    "preload.js",
    "package.json",
    "src",
    "renderer",
    "assets",
    "LICENSE",
    "NOTICE",
)

#: Never copied, whatever the source tree contains.
EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "tests",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
        "data",
        "run",
        ".arena",
    }
)

EXCLUDE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".sqlite", ".sqlite3", ".pcap", ".log")

#: Files that must exist in a finished portable build.
REQUIRED_FILES: tuple[str, ...] = (
    "UQAB.exe",
    "README.txt",
    "LICENSE",
    "resources/app/main.js",
    "resources/app/preload.js",
    "resources/app/package.json",
    "resources/app/renderer/index.html",
    "resources/app/renderer/renderer.js",
    "resources/app/renderer/style.css",
    "resources/app/src/api.js",
    "resources/app/src/engine.js",
    "resources/app/src/history.js",
    "resources/app/src/format.js",
    "resources/app/assets/icon.png",
    "resources/app/engine/app_api/server.py",
    "resources/app/engine/app_api/engine.py",
    "resources/app/engine/pt/client.py",
    "resources/app/engine/pt/server.py",
    "resources/app/engine/cover/scheduler.py",
    "resources/app/engine/config/config.yaml",
    "resources/app/engine/lab/stack.py",
    "resources/app/engine/dashboard/server.py",
    "resources/app/engine/experiments/compare.py",
    "resources/app/engine/persona/profiles/balanced.json",
    "resources/app/engine/tunnel/certs/ca.crt",
    "resources/app/engine/tunnel/certs/client.crt",
    "resources/app/engine/tunnel/certs/client.key",
    "resources/app/engine/tunnel/certs/server.crt",
    "resources/app/engine/tunnel/certs/server.key",
)

#: The Windows interpreter the launcher uses for the engine.
PYTHON_EXE = "resources/python/python.exe"

README = """UQAB - a Tor pluggable transport with persona-driven cover traffic
====================================================================

Run it
------
Double-click UQAB.exe. Nothing is installed and no administrator rights are
needed: the folder is the whole application. You may move it anywhere, including
a USB stick.

First start
-----------
1. The window shows "Disconnected". Pick a persona on the right (start with
   "balanced").
2. Press Start. If Tor is not installed, UQAB starts its own Lab simulation
   stack and says so with a "Lab simulation" badge - the transport, the shaping
   and the browser all work, but there are no real Tor circuits.
3. When the panel says "Protected", the address bar unlocks. Every page request
   goes through UQAB's SOCKS5 listener with remote DNS; nothing bypasses it.
4. Press Stop to end the session. Traffic stops with the tunnel.

Tor
---
To use a real Tor, install Tor Browser or a tor binary and point UQAB at it in
config\\config.yaml (tor.mode, tor.binary, tor.bridges) inside
resources\\app\\engine\\config. See docs\\LAB_MODE.md for exactly what lab mode
does and does not prove.

Clearnet learning
-----------------
Off by default. If you opt in from the panel, UQAB may learn a persona from a
capture you supply; the raw capture is overwritten and deleted, and only
aggregate numbers are stored. Withdraw at any time to delete the learned profile.

Evaluation
----------
The A/B/C experiment and the dashboard are Python entry points inside the bundle:

  resources\\python\\python.exe -m experiments.compare   (needs the engine on PYTHONPATH)
  resources\\python\\python.exe -m dashboard.server

See docs\\REPORT.md and docs\\PACKAGING.md for the details.

Licences
--------
The browser shell derives from Min (Apache-2.0); see resources\\app\\NOTICE and
resources\\app\\LICENSE. Electron/Chromium and CPython keep their own licences in
their directories.
"""


def is_excluded(path: Path) -> bool:
    """True when a path must never be copied into the bundle."""
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    return path.suffix.lower() in EXCLUDE_SUFFIXES


def iter_source_files(root: Path, item: str) -> list[Path]:
    """Every file under ``root/item`` that belongs in the bundle."""
    target = root / item
    if not target.exists():
        return []
    if target.is_file():
        return [] if is_excluded(Path(item)) else [target]
    out: list[Path] = []
    for candidate in sorted(target.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if is_excluded(relative):
            continue
        if candidate.name in {"ca.key", "client.key", "server.key"}:
            continue
        out.append(candidate)
    return out


def verify_layout(bundle: Path) -> list[str]:
    """Check a built bundle; returns a list of human-readable problems."""
    problems: list[str] = []
    if not bundle.is_dir():
        return [f"{bundle} does not exist"]
    engine = bundle / "resources/app/engine"
    for required in ("app_api", "pt", "tunnel", "cover", "protocol", "persona", "profile"):
        if not (engine / required).is_dir():
            problems.append(f"engine package missing: {required}")
    for name in REQUIRED_FILES:
        target = bundle / name
        if not target.is_file():
            problems.append(f"missing {name}")
        elif target.stat().st_size == 0:
            problems.append(f"empty {name}")
    for name in ("resources/app/engine/config/config.yaml",):
        target = bundle / name
        if target.is_file() and "paths:" not in target.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"{name} does not look like the UQAB configuration")
    if engine.is_dir():
        if any(engine.rglob("__pycache__")):
            problems.append("the bundle contains __pycache__ directories")
        for private in engine.rglob("*.key"):
            if private.name in {"ca.key"}:
                problems.append("the bundle ships the CA private key")
    if (bundle / "resources/app/engine/tests").exists():
        problems.append("the test suite was copied into the bundle")
    python = bundle / PYTHON_EXE
    if not python.is_file():
        problems.append(f"missing {PYTHON_EXE} (the bundled interpreter)")
    return problems
