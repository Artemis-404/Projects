"""UQAB lab simulation.

This package exists so the whole UQAB data path can be demonstrated and tested on
one machine without Tor or a private bridge.  It is *clearly labelled* in the app
as \"Lab simulation\" and it never reaches the public internet: the mock ORPort
only dials the bundled demo origin.

What is still real in lab mode:

* the managed PT v1 handshake between a Tor stand-in and ``pt/client.py``;
* the mutual-TLS 1.3 tunnel and certificate checks;
* session parameters, the shared timetable and fixed-size cover records;
* fail-closed behaviour and the byte counters the app displays.

What is simulated: Tor itself (SOCKS front end, circuits, relay network) and the
bridge's ORPort.  See ``docs/LAB_MODE.md``.
"""
