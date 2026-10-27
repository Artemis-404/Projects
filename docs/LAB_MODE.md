# Lab mode: what it proves and what it does not

UQAB ships with a Tor-free simulation stack (`lab/`) so the app, the transport and
the evaluation pipeline can be demonstrated on a machine where Tor is not
installed, cannot reach the network, or is blocked by the campus firewall. The UI
labels this mode **Lab simulation** everywhere, and every record in the database
keeps its configuration letter.

## The stack

```
  app / experiment                lab process group
  ---------------                 -------------------------------------------------
  SOCKS5 127.0.0.1:p  ─────────►  mock Tor (lab/mocktor.py)
                                     └─ starts the real PT client as a managed PT
                                        reads VERSION / CMETHOD from stdout
                                        SOCKS5 to the client with persona=<id>
                                          └─ mutual TLS 1.3  (tunnel/)
                                             └─ shaped records (cover/, protocol/)
                                                └─ PT server (pt/server.py)
                                                   └─ mock ORPort (lab/orport.py)
                                                      └─ demo origin (lab/origin.py)
```

* `lab/origin.py` serves ten demo pages on loopback (`/`, `/docs`, `/news`, ...).
* `lab/orport.py` speaks the lab stream header (`UQABLAB1` + host + port) and only
  accepts `demo.uqab` and `*.uqab` destinations, so a misconfigured run fails
  loudly instead of silently succeeding.
* `lab/mocktor.py` does exactly what Tor does for a managed client transport:
  launch it with the `TOR_PT_*` environment, parse `CMETHODS DONE`, then open a
  SOCKS5 stream per browser connection with the persona in the username field.
  For configuration A it instead relays straight to the origin (no PT).
* `lab/stack.py` wires it all together and writes `run/lab.json`
  (`{ready, mode: "simulated", persona, socks_port, ...}`).

Run it:

```bash
python -m lab.stack --persona high --socks-port 9150
# lab ready: socks 127.0.0.1:9150, bridge PT ..., origin ...
curl --socks5-hostname 127.0.0.1:9150 http://demo.uqab/
```

## What it does prove

1. The managed-PT contract works: `VERSION`, `CMETHOD`, `CMETHODS DONE`, the
   `TOR_PT_*` environment, and the persona-in-username convention.
2. The whole transport is exercised end to end over loopback: SOCKS5 with remote
   DNS, mutual TLS 1.3, frame parsing, fixed-size records, the scheduler's slot
   pacing, the fail-closed guard, and the stats file.
   From D21 the tunnel is **persistent and multiplexed**: pages that open several
   connections still produce exactly one record stream, and the stream keeps
   running (as cover) between page loads. The lab test asserts both.
3. Page loads through the shaped channel really return the bytes (4117 B for `/`,
   3720 B for `/docs` in the reference transcript) and do so inside the
   configured slot timing (p95 slot error ≈ 1 ms on the reference machine).
4. The evaluation pipeline runs end to end: shaped records become a capture,
   windows get ACTIVE/IDLE labels, both classifiers train with cross-validation,
   and the A/B/C table is produced.

## What it does not prove

* **Tor's anonymity properties.** There are no circuits, no relays, no onion
  layers. UQAB shapes a byte stream; it does not add anonymity by itself.
* **Resistance to a real network observer.** The lab observer sees loopback
  timing with no queueing, no cross-traffic and no MTU effects. Absolute F1
  numbers from lab runs are therefore optimistic; only the *relative* ordering of
  configurations A/B/C is meaningful, and the report must say so.
* **Real Tor interoperability.** A real bridge run needs a Tor binary, a reachable
  bridge and a real destination. `app_api/tor_control.py` implements that path
  (bootstrap parsing, cookie authentication, shutdown) and the code paths are
  shared, but it cannot be verified in a sandbox without Tor.

## The lab timeline: why phases are jittered and loads repeat

The observer in these experiments sees one thing only: the size and direction of
each record, one per slot. That makes a naive script easy to defeat — or, worse,
easy to *win* for the wrong reason. Two artefacts had to be removed before any
A/B/C number meant anything:

1. **Phase boundaries on the analysis grid.** With a perfectly regular script
   (`load_s`, `idle_s` exactly), every 1 s feature window falls either wholly
   inside a phase or exactly on a boundary, so a classifier can separate ACTIVE
   from IDLE by *where the window sits* rather than by what the traffic did. The
   replay therefore draws `experiments.jitter_s` (default 400 ms) of uniform
   jitter on every phase, and the ground truth records the duration that actually
   elapsed, not the duration that was asked for.
2. **Loads shorter than a window.** On loopback a page arrives in a few
   milliseconds, so an ACTIVE phase contained almost no traffic and was labelled
   ACTIVE anyway — the labels were noise. The demo origin now paces its body in
   1 KiB chunks (`chunk_delay_s = 20 ms`) and one ACTIVE phase is a *page view*:
   up to `experiments.requests_per_load` (default 24) sequential requests with
   short gaps, stopping when the phase budget is used. A browser fetching a
   document plus subresources looks like this; a single 4 ms request does not.

Both numbers are configuration, not code: `--loads`, `--idle`, `--repeats` on
`scripts/demo.py` and the same keys in `config/config.yaml` reproduce a run.

**Records are not packets.** In the lab, one observation is one *record*
(`cover.record_size`, up to 3932 B), and `analysis/pcap.py` writes and reads
those sizes faithfully (Ethernet + IPv4/IPv6 headers included, payload up to
65 535 B). A real tcpdump capture of the same tunnel would instead show MTU-sized
(≈1448 B) segments carrying slices of the TLS stream. Do not compare lab record
sizes against interface MTU, and do not read a lab `length` column as "bytes on
the wire" — the wire here is loopback with a 16 KiB MTU.

## Configuration A in lab mode

Configuration A is "plain Tor". In the lab there is no Tor to be plain *with*, so
the mock Tor connects straight to the demo origin and logs each relayed chunk to
`run/logs/plain-client.jsonl`. Treat configuration A's laboratory numbers as a
*pipeline baseline* (no shaping, single RTT, no cover), not as a measurement of
the Tor network. Real-network A/B/C runs are the ones the report should quote;
the lab runs are what makes the project demonstrable offline.

## Where the numbers come from

| artefact | path |
| --- | --- |
| shaped record log (uplink) | `run/logs/slots-client.jsonl` |
| shaped record log (downlink) | `run/logs/slots-server.jsonl` |
| plain relay log (config A) | `run/logs/plain-client.jsonl` |
| live counters (1 Hz) | `run/stats.json` |
| lab readiness | `run/lab.json` |
| per-run artefacts | `<paths.experiments>/<run_id>/` (`capture.pcap`, `features.csv`, `ground-truth.csv`, `loads.json`, `summary.json`) |
| aggregates | `data/uqab.sqlite` |
