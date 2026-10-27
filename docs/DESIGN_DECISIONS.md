# UQAB design decisions and deviations

This file records every place where the implementation makes a choice the SDD
leaves open, or deliberately departs from it. Each entry states the decision, the
reason, and what the report should say about it.

## D1 — Managed pluggable transport, not a bridge plugin

UQAB is a *managed PT* (Tor starts it, reads `CMETHOD`/`SMETHOD` from stdout).
Rationale: no C in Tor, no build against Tor headers, and the same binary runs on
Windows. The PT speaks SOCKS5 with the persona id in the username field, so Tor's
`Bridge` line carries the persona (`persona=high`).

## D2 — Fixed record size, padding inside the record

Every record on the wire is exactly `record_size` bytes: DATA frames first, then
one PADDING frame filling the remainder. The record is written with a single
`write()` call. This is what makes length-based classification useless: the
observer sees one constant length per persona state, not a sequence of TLS
records of varying size.

*Implementation note:* the DATA payload is capped at `record_size - 3` so a
PADDING header always fits, even when the record is completely full.

## D3 — SOCKS5 hop between mock Tor and the PT client

In real Tor, Tor connects to the PT's SOCKS listener. The lab does the same thing
(`lab/mocktor.py`), so the production code path is exercised end to end; only
Tor's own circuits are absent.

## D4 — Persona transitions are partial rows

A persona may omit targets with probability zero instead of writing them
explicitly. Missing targets are treated as weight 0 and the row is validated on
the targets that remain. This keeps hand-written and LLM-written personas short
without weakening validation.

## D5 — Greeting record size is persona-independent

The session-start PING travels in a record of `cover.greeting_record_size`
(default 1024 bytes), whatever the persona says. Rationale: the greeting must
identify the persona (id + SHA-256 of the persona document + seed) *before* the
schedule starts, so its length cannot depend on the schedule. The consequence is
documented for the report: the first record of a session is not shaped by the
persona, and the observer can therefore see one fixed-length record at session
start.

## D6 — Constant persona for configuration B

Configuration B ("constant-rate cover") is the static `constant` persona: one
state, one record size, one slot interval. It is not a separate code path — the
scheduler treats it like any other persona, which keeps the comparison honest.

## D7 — Configuration A in lab mode is a plain relay

When the lab runs configuration A, the mock Tor connects straight to the demo
origin with no pluggable transport (`MockTor(plain=True)`). Its byte log
(`run/logs/plain-client.jsonl`) is turned into the same packet stream as the
shaped logs, so the same feature extractor and classifiers score all three
configurations. `docs/LAB_MODE.md` states clearly that this measures the
*pipeline*, not the real Tor network.

## D8 — Observers are implemented in plain Python

The SRS asks for logistic regression and a random forest. scikit-learn would add
a large binary dependency to the portable Windows build, so both models are
implemented in `analysis/classifier.py`: L2 logistic regression trained with
gradient descent, and a bagged CART forest with Gini impurity and random feature
subsets. Cross-validation is stratified and the F1 confidence interval is a
percentile bootstrap, matching NFR-05.

## D9 — Lab capture comes from the slot log, not from pcap

The sandbox (and most student machines) cannot run `tcpdump` without root. Every
shaped record is already logged with `(timestamp, size, state)` by `SlotRecorder`,
so the experiment converts those logs into a packet stream
(`experiments/capture.py`) and writes a real pcap file. On a machine with
`tcpdump`, the same analysis code reads the pcap instead; the reader and the
feature extractor are identical.

## D10 — Persona learning is opt-in and deletes the capture

Clearnet learning requires the consent record (`app_api/consent.py`) to be
accepted; the importer refuses to run otherwise and keeps the raw capture. When
it does run, the capture is overwritten with zeros and unlinked, and the profile
is aggregates only. `profile/privacy.py` re-checks a finished profile (and is used
as a test oracle) for URLs, host names, IP addresses, e-mail addresses and free
text.

## D11 — The dashboard is a stdlib HTTP server

The SDD names Streamlit. Shipping Streamlit inside a portable `.exe` would add
hundreds of megabytes and a second web stack, so the dashboard is a read-only
`http.server` app with inline SVG charts (`dashboard/`). The pages, routes and
data are the ones the SDD specifies; only the framework differs.

## D12 — The browser is Min-derived, not a literal fork

The user-facing app is built in Min's architecture (Electron main process,
`<webview>` tabs, address bar, history) with UQAB branding throughout; Min's
Apache-2.0 `LICENSE` and `NOTICE` are vendored under `app/`. It is **not** a
byte-for-byte fork of Min: Min's TypeScript build, its dependency tree and its
task-tab feature are not carried over. The report must state this deviation
honestly rather than claiming a fork. What matters for FR-26..FR-31 — all traffic
through SOCKS5 with remote DNS, WebRTC UDP disabled, no own network calls, a
single Start/Stop control, a locked persona picker while running, the 1 Hz panel,
clearnet opted-in only, UQAB branding — is implemented.

## D13 — Effective configuration files for child processes

Every child process (PT client, PT server, mock Tor, lab stack) is started with
the *same* effective configuration file the parent wrote
(`config.write_effective_config`), with all file paths made absolute. Without this
a redirected run or certificate directory applies on one side of the tunnel only
and the session stalls. This was a real bug during development; the pattern is
now mandatory for any new child process.

## D14 — Fail-closed everywhere

Any parse error, size violation, TLS failure, persona mismatch or internal error
closes the tunnel. There is no cleartext fallback and no "continue with reduced
protection" path (`security/guard.py`). The guard's state is exported through the
stats file so the panel can show `blocked` instead of pretending to be connected.

## D15 — Lab mode is labelled in the UI and in the data

The app never pretends to be using Tor when it is not. `run/lab.json` and the
stats file carry `mode: "simulated"`, the panel shows a "Lab simulation" badge,
and every experiment row is marked with its configuration letter so a report
reader can tell which numbers came from the lab.

## D16 — Installers for both platforms, not one portable folder

The phase plan asked for "a Windows artifact". Real use needs an installable app:
the Windows build is an NSIS setup (`perMachine: false`, no elevation) and Linux
gets an AppImage plus a `.deb`, all produced by electron-builder from
`packaging/electron-builder.json`. The portable zip is kept as a third artifact
because it is the fastest way to demo the app on a locked-down machine. Both
builders copy the engine and the Python runtime through the same manifest, so the
three artifacts cannot disagree about what the app is.

## D17 — Settings live in the app, not in config.yaml

The end user never edits YAML. The first-run wizard writes `settings.json` in the
user-data directory; `app/src/settings.js` turns it into dotted overrides
(`tor.mode`, `tor.binary`, `tor.socks_port`, `tor.bridges`, `persona.default`,
`llm.url`, `llm.model`); the main process starts the control API with
`--settings <file>`; `app_api/server.py` applies them with `Config.with_overrides`
and hands the *effective* configuration to every child process. Saving a setting
restarts the engine and re-gates the tabs, so there is never a half-applied state.
`config/config.yaml` remains the shipped default that the wizard starts from.

## D18 — Tor is optional but always reported

No user has to install Tor to try UQAB, and no user is ever misled about what is
running. `GET /tor` reports whether a binary was found, where, and its version;
the wizard uses it to preselect *Automatic* and to explain the fallback; the
session badge says *Tor* or *Lab simulation*; every run row records the mode. This
is the same honesty rule as D15, moved earlier: into setup.

## D19 — The lab protocol is jittered and paced like a browser

A scripted timeline that is perfectly regular measures the script, not the
transport: phase boundaries land on the 1 s analysis grid and a loopback page
arrives in fewer milliseconds than a window is wide, which produced identical
"perfect" F1 scores for A, B and C in early demo runs. Every phase now carries
`experiments.jitter_s` of uniform jitter (ground truth records the elapsed
duration), the demo origin paces its body in 1 KiB chunks, and one ACTIVE phase
is a page view of up to `experiments.requests_per_load` sequential requests that
stops when the phase budget is used. The labels describe the traffic that really
happened, and repeated runs (`--repeats`) let the report quote run-to-run
variance instead of one lucky split.

## D20 — Captures are byte-faithful to records

`analysis/pcap.py` used to clamp a payload to 1400 B and to report the IPv4
`total_len` as the frame size, so a 3932 B record read back as 1440 B and every
size feature silently saturated. The writer now takes the full 65 535 B IP
payload, and the reader returns the frame size (Ethernet + IP + TCP/UDP) for both
IPv4 and IPv6. A record of *n* bytes round-trips as *n*; `tests/test_profile.py`
pins sizes from 64 B to 16 KiB. Honest sizes are a precondition for honest
observability numbers, so this is a correctness fix, not a cosmetic one.

## D21 — One persistent, multiplexed tunnel per client (not one per stream)

The pre-D21 transport opened a shaped session for **every** Tor stream. That is
easy to implement and wrong for the research question: while the user idles there
is no UQAB traffic at all, and when a page loads six connections the observer sees
six parallel record streams whose aggregate rate jumps six-fold. Both facts leak
more than the persona hides, and neither is something the persona timetable can
compensate for.

The transport now keeps **one** TLS tunnel and **one** record stream per client:
it is opened on the first SOCKS request, carries the session parameters once, and
then runs the slot clock until the engine stops. Tor streams are multiplexed
inside it with three frame types —

| frame | payload | meaning |
| --- | --- | --- |
| `STREAM_OPEN` (0x05) | `StreamId | "host:port"` | a Tor stream begins |
| `STREAM_DATA` (0x06) | `StreamId | bytes` | bulk data, fragmented over records |
| `STREAM_END` (0x07) | `StreamId` | one direction is finished |

— while `PING`/`CLOSE` keep their session-level meaning and `PADDING` still fills
every record to `record_size`. The client allocates odd stream ids, the bridge
even ones.

Consequences that are deliberately accepted:

* **Cover never stops.** IDLE now looks exactly like ACTIVE from the wire: same
  record sizes, same slots. That is the whole point, and it is what makes the
  A/B/C comparison meaningful (see `docs/report/sample-results.md`).
* **Cost is per second, not per byte.** A continuously shaped tunnel spends
  cover bytes while nothing is happening, so "overhead = wire bytes / page bytes"
  grows as the user browses less. The comparison table therefore also reports
  wire kbit/s and IDLE B/s; both are recorded per run
  (`runs.wire_bps`, `runs.idle_bps`, schema v2 with an in-place migration).
* **One persona per session.** The persona is negotiated in the greeting; a
  second stream asking for a different persona is carried on the session's
  persona and the mismatch is logged. Rotating personas mid-session would itself
  be a fingerprint.
* **A dead tunnel is still fail-closed.** Every stream inside the tunnel is
  closed when the session fails (`tests/test_mux.py`), and the next SOCKS request
  re-opens the tunnel with the same retry policy as before.

`STREAM_END` is queued **behind** that stream's bytes, never ahead of them: an END
that overtakes its own data truncates the response (this was caught by the lab
test, which now asserts a full 4117-byte page arrives through the shaped path).

## D22 — No WSL2 or virtual machine: the engine runs natively

UC-7 and NFR-14 in the SRS describe a client engine that runs in WSL2 (Ubuntu
24.04) behind a Windows app. UQAB does not use WSL2 or any VM. The engine is
standard-library Python, so the installers bundle a self-contained CPython
(`packaging/build_installer.py`) and the app starts it directly as a child process
(`app/src/engine.js`). On Windows nothing else has to be installed or enabled: no
WSL feature, no Linux distribution, no virtualisation, no admin rights.

The same engine still runs natively on Ubuntu (AppImage / `.deb`, and the CI job
on `ubuntu-latest`), so the Linux half of NFR-14 is kept; only "WSL2 on Windows"
is dropped. UC-7's step "app checks the client engine in WSL2" becomes "app starts
the bundled engine and waits for `/health`". The report should state this as a
deliberate SRS change: it removes the hardest install step for end users and
removes a second operating system from the trusted path.

## D23 — Servers close without waiting for live connections (Python 3.12+)

Since Python 3.12.1, `asyncio.Server.wait_closed()` — and therefore
`serve_forever()` when cancelled and `async with server` — waits until every
accepted connection has ended. A PT server with a live tunnel, or a PT client with
open Tor streams, could therefore never finish shutting down; on Windows the
multiplexing test hung for good. The listeners now wait on a plain future instead
of `serve_forever()`, and `security.guard.close_server()` aborts live connections
before a *bounded* `wait_closed()`. The SRS targets Python 3.12+ (NFR-14), so this
is a correctness fix for the supported runtime, not a test workaround.

## D24 — The slot clock uses a high-resolution timer on Windows

NFR-03 (p95 slot error ≤ 5 ms) failed on Windows at about 19 ms. Two causes: the
event-loop clock there (`time.monotonic()`, GetTickCount64 before Python 3.13)
moves in ~15.6 ms steps, and asyncio timeouts are quantised to the same system
timer. The slot clock now schedules and measures with `time.perf_counter()` and,
on Windows only, waits in `cover.scheduler.PreciseTimer`: one daemon thread that
sleeps with `time.sleep` (a high-resolution waitable timer since Python 3.11) in
2 ms slices and wakes each waiter on its own loop. Measured on the Windows 11
development machine: p95 1.3–2.3 ms at 25 ms and 100 ms slots, against 15–19 ms
before. `timeBeginPeriod(1)` was tried first and rejected: Windows 11 honoured it
in only one of six runs for a windowless process. Logged slot times stay on the
`time.monotonic()` epoch so they still line up with the experiment's ground truth.
