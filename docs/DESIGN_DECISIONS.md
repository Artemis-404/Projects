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
