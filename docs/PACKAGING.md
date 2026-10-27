# Packaging and installers

UQAB ships as a normal desktop application. There is nothing to configure by hand:
the installer places the app, and the first-run **Setup** screen asks the only
question that matters — *how should UQAB reach the network?* — then the app works.

Three artifacts are produced from this repository:

| artifact | command | what the user gets |
| --- | --- | --- |
| **Windows installer** | `python packaging/build_installer.py --win` | `UQAB-Setup-1.0.0.exe` — per-user install, no admin prompt, Start-menu + desktop shortcut, uninstaller |
| **Linux packages** | `python packaging/build_installer.py --linux` | `UQAB-1.0.0-linux-x86_64.AppImage` (double-click) and `UQAB-1.0.0-linux-amd64.deb` (`apt install ./…deb`, application-menu entry) |
| **Portable folder** | `python packaging/build_portable.py` | `UQAB-portable-win64.zip` — unzip anywhere, run `UQAB.exe`, no install at all |

The two builders share one manifest (`packaging/manifest.py`), so the engine copy
and the verification rules cannot drift apart.

## What every artifact contains

```
UQAB (installed: %LOCALAPPDATA%\Programs\UQAB, /opt/UQAB, or the portable folder)
  UQAB.exe                     the app (Electron launcher / AppImage / deb binary)
  resources/
    app.asar                   our app: main.js, preload.js, renderer/, src/, assets/
    engine/                    the complete Python engine (packages, personas,
                               config, docs, pre-generated lab certificates)
    python/                    self-contained CPython 3.11 (no Python install needed)
```

Consequences the user notices:

* **No Python, no Tor, no Node.** Python is bundled; Tor is detected if present and
  otherwise replaced by the clearly-labelled lab simulation.
* **No admin rights.** The Windows installer is per-user (`perMachine: false`,
  `allowElevation: false`); the user may choose the folder.
* **No config files.** Every setting lives in the Setup screen (see
  `docs/USER_GUIDE.md`) and is stored in the app's user-data directory:
  * `settings.json` — what the wizard saved;
  * `engine-settings.json` — the dotted overrides handed to the engine.

## How the GUI settings reach the engine

The wizard writes `settings.json`, converts it to dotted overrides
(`app/src/settings.js` → `toOverrides`) and the main process starts the control API
with `--settings <userData>/engine-settings.json`. `app_api/server.py` applies them
with `Config.with_overrides`, then writes an *effective* configuration file that
every child process (PT client, PT server, mock Tor, lab stack) also reads. One
source of truth, from the checkbox to the wire.

Changing a setting restarts the engine process and re-gates the browser tabs, so a
half-applied configuration is impossible.

## Building the Windows installer

On a Windows machine (or a Linux/CI machine with Wine for the NSIS step):

```bat
git clone <this repository> && cd Projects
python scripts\make_icon.py
python packaging\build_installer.py --win
```

* `npx electron-builder@26` is fetched automatically (needs Node.js + network once).
* The CPython runtime comes from `astral-sh/python-build-standalone` and is cached
  in `build/cache`; pass `--python-archive <file>` to build offline.
* Output: `build/dist/UQAB-Setup-<version>.exe`, plus `build/dist/build-report.json`.

The installer is unsigned, so Windows SmartScreen shows "unknown publisher" on
first run. Code signing needs a certificate the project does not have; the report
should say so rather than pretend otherwise.

## Building the Linux packages

```bash
python packaging/build_installer.py --linux
# build/dist/UQAB-1.0.0-linux-x86_64.AppImage
# build/dist/UQAB-1.0.0-linux-amd64.deb
```

* AppImage: `chmod +x UQAB-*.AppImage && ./UQAB-*.AppImage`.
* deb: `sudo apt install ./UQAB-*.deb`, then launch "UQAB" from the application menu
  (desktop entry: `Category=Network`, `StartupWMClass=UQAB`).
* The deb declares its runtime dependencies in `packaging/electron-builder.json`
  (`deb.depends`).

## Verifying a build without packing

```bash
python packaging/build_installer.py --check      # config sanity (no admin, targets, icons)
python packaging/build_installer.py --no-run     # stage engine + python, verify layout
python packaging/build_portable.py --check-only  # verify an existing portable bundle
```

`tests/test_installer.py` and `tests/test_packaging.py` run the same staging code
against stub archives, so a broken copy list fails in CI rather than on a user's
machine.

## First run on the user's machine

1. Install (or unzip) and start UQAB.
2. The Setup wizard opens: **Automatic** is preselected. It reports whether a Tor
   binary was found (`GET /tor`), and if not, says plainly that the lab simulation
   will be used.
3. Chosen settings are saved; the engine restarts; the panel shows *Disconnected*.
4. Pick a persona, press **Start**, wait for *Protected*, browse.
5. Reopen Setup any time from the right-hand panel ("Setup: connection & Tor").

## Honest limitations

* Release binaries cannot be downloaded in the development sandbox
  (`objects.githubusercontent.com` is blocked), so the real installers are built on
  the user's machine or CI. Everything up to the packager — staging, layout
  verification, the engine smoke test — runs and is tested here.
* No auto-update, no crash reporting, no telemetry.
* Windows and Linux only; macOS is not targeted.
* The bundled lab simulation is a *demonstration* stack, not Tor: see
  `docs/LAB_MODE.md` for exactly what it does and does not prove.
