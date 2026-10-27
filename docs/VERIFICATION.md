# Verification: what was actually run, and what was not

This page exists so a reader (or an assessor) can tell the difference between
"implemented", "tested here" and "needs a real machine". Everything below was run
in a plain Linux container with Python 3.11 and Node 22 — no Tor, no network
access to GitHub releases, no Windows.

## 1. Automated gates

| command | result |
| --- | --- |
| `python -m ruff check .` | clean |
| `python -m mypy .` | `Success: no issues found in 84 source files` |
| `python -m pytest -q` | 334 passed (`tests/`) |
| `cd app && node --test test/*.test.js` | 43 tests, 43 passed |
| `python packaging/build_installer.py --check` | `{"problems": []}` |
| `python -m pytest --cov` (core packages) | ≥ 70 %: `cover/`, `protocol/`, `analysis/`, `persona/`, `storage/`, `security/` |

The suite is behavioural where it matters: TCP round trips through the real
transport (`tests/test_session.py`, `tests/test_mux.py`), the full lab stack with
mock Tor and mock ORPort (`tests/test_lab.py`), the SQLite comparison
(`tests/test_experiment.py`), the control API over HTTP (`tests/test_api.py`) and
the packaging stager against a synthetic CPython archive
(`tests/test_packaging.py`, `tests/test_installer.py`).

## 2. End-to-end demonstrations

### The shaped tunnel, with a real page

`tests/test_lab.py::test_lab_stack_serves_pages_through_the_shaped_tunnel` starts
the whole stack (mock Tor → PT client → mutual TLS → PT server → mock ORPort →
demo origin), fetches `http://demo.uqab/` through the SOCKS port and asserts the
complete 4117-byte page arrives, inside fixed-size records, with the fail-closed
guard armed. A fetch of `http://example.com/` through the same port is refused,
which is how the lab proves it is not an open proxy.

### Six tabs, one tunnel

`tests/test_mux.py::test_parallel_streams_share_one_tunnel` opens six concurrent
SOCKS streams and asserts the bridge counted **one** session: the observer sees
one record stream, not six.

### Cover during idle

`tests/test_mux.py::test_cover_keeps_running_after_the_last_stream` closes every
stream, waits, and asserts the stats file keeps incrementing `cover_up` while the
state stays `connected`. `test_tunnel_is_up_before_the_first_page` asserts the
tunnel exists before any page is opened — without that, the app's tab gate could
never open and "IDLE" would be silence.

### A/B/C evaluation

```bash
python scripts/demo.py --loads 12 --idle 2 --repeats 3
```

runs configurations A, B and C three times each through the lab, renders the
dashboard, and writes `docs/report/sample-results.md` (+ `.json`). The committed
report is the output of that command; the numbers are not typed by hand.

## 3. What could **not** be verified here

| item | why | how to verify |
| --- | --- | --- |
| Real `.exe` / AppImage / `.deb` artifacts | the Electron runtime is downloaded from GitHub releases, which this sandbox cannot reach (`fetch failed`); `npm install` fetches the package but not the binary | `python packaging/build_installer.py --win` on Windows, `--linux` on Linux, then run the produced installer |
| The Electron window itself | same missing runtime — the renderer, preload and main-process logic are covered by the 43 Node tests, but no window was rendered here | `cd app && npm install && npm start` |
| Real Tor and real bridges | no Tor binary, no network egress, no bridge | set *Tor mode → Always use my Tor binary* in the setup wizard |
| Real network conditions | the lab is loopback with no cross traffic, no queueing, no MTU effects | run the evaluation on a real bridge, see `docs/LAB_MODE.md` |

## 4. Reproducing this page

```bash
python -m ruff check .
python -m mypy .
python -m pytest -q
cd app && npm install && node --test test/*.test.js && cd ..
python packaging/build_installer.py --check
python scripts/demo.py --loads 12 --idle 2 --repeats 3
```
