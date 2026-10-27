# Building the portable Windows artifact

FR-31 asks for something the user can *run on Windows with a simple `.exe`*, with
no installer and no administrator rights. The build produces exactly that: a
folder named `UQAB` (and a zip of it) whose `UQAB.exe` starts the app.

## What is inside

```
UQAB/
  UQAB.exe                     Electron launcher, renamed (no installer, no admin)
  README.txt                   how to run it, what lab mode means
  LICENSE                      UQAB licence + third-party pointer
  build-info.json              electron/python versions, git commit, build time
  chrome_*.pak, *.dll, ...     Electron/Chromium runtime files
  resources/
    app/                       the UQAB app (main.js, preload.js, src/, renderer/)
      engine/                  the complete Python engine
        app_api/ pt/ tunnel/ cover/ protocol/ security/ persona/ profile/
        analysis/ experiments/ storage/ dashboard/ lab/ config/
        config/config.yaml     the shipped configuration
        persona/profiles/      static personas (+ generated/)
        tunnel/certs/          pre-generated mutual-TLS certificates (no CA key)
        docs/                  design decisions, lab mode, packaging
      README.md                the repository readme
      LICENSE, NOTICE          Min's Apache-2.0 licence and the attribution
    python/                    self-contained CPython 3.11 (python.exe + stdlib)
```

Everything is relative to the folder, so the app runs from any path, including a
USB stick. User state (history, learned profile, runs, the SQLite database) is
written under the OS user-data directory or `run/`/`data/` next to the engine,
never inside the Electron runtime.

## Build

```bash
python packaging/build_portable.py
```

Steps it performs:

1. **Download** (cached in `build/cache/`):
   * `electron-v<version>-win32-x64.zip` from `electron/electron`;
   * `cpython-3.11.*-x86_64-pc-windows-msvc-install_only.tar.gz` from
     `astral-sh/python-build-standalone` (the runtime Astral ships for `uv`).
2. **Assemble** `build/UQAB`:
   * unzip Electron, rename `electron.exe` → `UQAB.exe`, drop `default_app.asar`;
   * unzip CPython into `resources/python`, flatten its `python/` prefix, and trim
     `Lib/test`, `Lib/tkinter`, `Lib/idlelib`, `Lib/lib2to3` and `__pycache__`;
   * copy the app (`packaging.manifest.APP_ITEMS`) and the engine
     (`ENGINE_ITEMS` + `ENGINE_DATA`), skipping tests, caches, `data/`, `run/`
     and every `*.key` except the leaf keys the tunnel needs;
   * generate the mutual-TLS certificates inside the bundle with
     `tunnel.certs --out` (the CA *private* key is deliberately not shipped);
   * write `README.txt`, `LICENSE` and `build-info.json`.
3. **Verify** with `packaging.manifest.verify_layout`: required files present and
   non-empty, engine packages complete, no `__pycache__`, no `tests`, no CA key.
4. **Smoke-test the engine copy**: import `app_api.server`, `pt.client`,
   `pt.server`, `lab.stack`, `experiments.compare`, `dashboard.server`,
   `analysis.classifier`, `profile.learner`, `persona.llm` and `storage.db` from
   the bundle with the host interpreter. This catches a missing package long
   before the .exe is shipped.
5. **Zip** to `build/UQAB-portable-win64.zip`.

Useful flags: `--no-download` (assemble from cache), `--electron-zip` /
`--python-archive` (supply the archives from disk), `--no-zip`, `--check-only`
(verify an existing bundle), `--no-smoke`.

## How the app finds Python on Windows

`app/src/engine.js` resolves the interpreter in this order:

1. `UQAB_PYTHON` environment variable (a full command line is allowed, so a venv
   can be used during development);
2. `resources/python/python.exe` — the bundled runtime in a packaged build;
3. `.venv\Scripts\python.exe` in the working tree (development);
4. `python` from `PATH`.

The control API is then started as `python -u -m app_api.server --host 127.0.0.1
--port <free port>` with `PYTHONPATH` set to the engine root, and the app waits
for `/health` before showing a session.

## Configuration on the user's machine

`resources\app\engine\config\config.yaml` is the shipped configuration and can be
edited after unzipping. The values a user is most likely to change:

| key | meaning |
| --- | --- |
| `tor.mode` | `auto` (Tor if found, else lab), `external`, `simulated` |
| `tor.binary` | absolute path to `tor.exe` |
| `tor.bridges` | bridge lines, e.g. `192.0.2.10:9443 AAAA...` |
| `tor.socks_port` | the port the browser will use (9150 by default) |
| `persona.default` | the persona selected at startup |
| `llm.url` | local model endpoint for persona generation (Ollama by default) |

## Reproducing the numbers on Windows

```bat
cd UQAB\resources\app\engine
set PYTHONPATH=%CD%
..\..\python\python.exe -m experiments.compare --loads 30
..\..\python\python.exe -m dashboard.server --port 8501
..\..\python\python.exe -m experiments.report --out docs\report\results.md
```

Run the comparison once with Tor installed (`tor.mode: external`) for the numbers
the report quotes, and once without for a first sanity check of the pipeline.

## Honest limitations

* The bundle is built *on the user's machine or a CI runner with network access*.
  In the development sandbox, `github.com` release assets
  (`objects.githubusercontent.com`) are blocked, so the real Electron/CPython
  binaries cannot be fetched here; the assembly, verification and smoke-test steps
  are exercised against stub archives by `tests/test_packaging.py` instead.
* No code signing. Windows SmartScreen will warn on first run; a signed build
  needs a certificate, which a course project does not have.
* The artifact is a folder-based portable app, not an installed application: it
  does not register file associations, add Start-menu entries or auto-update.
