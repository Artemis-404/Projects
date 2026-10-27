# Handoff: pick this up on your own machine

Everything in the SRS is implemented and the automated gates pass (see
`docs/VERIFICATION.md`). What remains cannot be done inside a sandbox: it needs a
machine that can download the Electron runtime and, for the Windows build, Windows
itself.

## 1. Clone and set up (5 minutes)

```bash
git clone <this-repo> uqab && cd uqab
python -m venv .venv
# Windows: .venv\Scripts\activate        Linux/macOS: . .venv/bin/activate
pip install -e .

python -m tunnel.certs        # mutual-TLS certificates (gitignored, generated here)
python scripts/make_icon.py   # icon.png + icon.ico (idempotent)
```

Run the gates yourself before changing anything:

```bash
python -m ruff check .
python -m mypy .
python -m pytest -q
cd app && npm install && node --test test/*.test.js && cd ..
```

## 2. See it work without Tor (10 minutes)

```bash
python scripts/demo.py --loads 6 --idle 2
```

This runs the A/B/C experiment through the lab stack and rewrites
`docs/report/sample-results.md`. It prints the comparison table, proves the
dashboard renders, and takes a few minutes.

To use the app in lab mode:

```bash
python -m app_api.server --host 127.0.0.1 --port 9170   # control API
cd app && npm start                                     # the browser
```

The first start shows the setup wizard. Choose *Automatic* (Tor if installed,
otherwise the built-in lab) and finish; press **Start**; browse. The tabs are
hard-gated on the tunnel: nothing loads until the session reports `connected`,
and the badge says whether you are on Tor or in the lab.

## 3. Build the installers (needs network)

```bash
python packaging/build_installer.py --check   # validate the config, no network
python packaging/build_installer.py --win     # build/dist/UQAB-Setup-1.0.0.exe
python packaging/build_installer.py --linux   # AppImage + .deb
```

* The Windows `.exe` is an NSIS setup: per-user, **no administrator prompt**,
  install-directory choice, Start-menu + desktop shortcuts, normal uninstall.
* Linux produces a single-file `AppImage` and a `.deb` with a menu entry.
* Both bundle the Python engine runtime, so the end user installs nothing else.
* `python packaging/build_portable.py` still produces the no-install zip.

The first run of either builder downloads Electron and CPython once and caches
them under `build/cache/`. If a download is blocked, the script says which URL it
needed; nothing else in the build path needs the network.

## 4. Verify the artifacts

1. Install the `.exe` (or run the AppImage/`.deb`) on a clean machine or VM.
2. Confirm the wizard appears, with *Automatic* preselected and the detected Tor
   shown if Tor is installed.
3. Press **Start**, load a page, and check the panel: state `connected`, persona
   name, cover bytes increasing, overhead number.
4. Stop the session and confirm the tabs stop loading (the gate is real).
5. Uninstall and confirm user data is left intact (settings survive by design).

## 5. What to do next, in order of value

1. **Run the A/B/C experiment on a real bridge** (`python -m experiments.compare
   --no-lab`) — the lab numbers in `docs/report/sample-results.md` are honest but
   loopback-only, and the report should quote real-network runs.
2. **Fill in `docs/REPORT.md`** with the real numbers (section 5) and answer the
   research question in the three sentences section 9 asks for.
3. **Persona tuning** if the overhead is too high for your taste: `high` is the
   expensive default for configuration C; `balanced`/`low` trade width for cost.
4. **Optional**: sign the Windows installer (an unsigned NSIS setup triggers
   SmartScreen) and add a `.desktop` MimeType if you want UQAB to be a default
   browser candidate.

## 6. Where things live

| path | what |
| --- | --- |
| `pt/client.py`, `pt/mux.py`, `pt/server.py` | the transport: SOCKS5, one persistent multiplexed shaped tunnel |
| `cover/`, `protocol/`, `tunnel/`, `security/` | records, frames, TLS, fail-closed guard |
| `persona/`, `profile/` | personas (static + local LLM) and the clearnet learner |
| `experiments/`, `analysis/`, `storage/`, `dashboard/` | evaluation pipeline and report |
| `app/` | Electron shell (Min-derived), setup wizard, settings |
| `app_api/` | control API the app talks to (`/status`, `/start`, `/tor`, `/setup`, …) |
| `lab/` | mock Tor, mock ORPort, demo origin — the Tor-free fallback |
| `packaging/` | portable zip and installer builders + electron-builder config |
| `docs/DESIGN_DECISIONS.md` | D1–D21, including why the tunnel is persistent (D21) |
| `docs/LAB_MODE.md` | what the lab proves and what it does not |
| `docs/VERIFICATION.md` | exactly what was run here, and what could not be |
