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
