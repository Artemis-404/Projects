"""Bundled demo origin for lab mode.

A small, dependency-free web server with ten pages so the browser has something
real to load while the shaped tunnel is exercised.  It only ever listens on
loopback and it is the *only* destination the mock ORPort will dial.
"""

from __future__ import annotations

import html
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEMO_DOMAIN = "demo.uqab"

STYLE = """
:root{--bg:#0b1020;--panel:#141b34;--ink:#e8ecf8;--muted:#93a0c4;--accent:#4de2c5;
--accent2:#7c8cff;--line:#243055}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:18px 28px;
border-bottom:1px solid var(--line);background:rgba(20,27,52,.7);
position:sticky;top:0;backdrop-filter:blur(6px)}
.logo{width:38px;height:38px}header b{font-size:18px;letter-spacing:.4px}
nav{margin-left:auto;display:flex;gap:18px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;font-size:14px}
nav a:hover{color:var(--accent)}main{max-width:880px;margin:0 auto;padding:36px 24px 80px}
h1{font-size:34px;line-height:1.2;margin:0 0 8px}
.sub{color:var(--muted);margin-bottom:28px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:22px;margin:18px 0}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;
background:rgba(77,226,197,.12);color:var(--accent);border:1px solid rgba(77,226,197,.3)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600}
footer{color:var(--muted);font-size:13px;padding:26px;text-align:center;
border-top:1px solid var(--line)}
code{background:#0f1730;border:1px solid var(--line);border-radius:6px;padding:1px 6px;
font-size:13px}
.gallery{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.tile{aspect-ratio:4/3;border-radius:12px;border:1px solid var(--line)}
"""

LOGO = (
    '<svg class="logo" viewBox="0 0 48 48" aria-label="UQAB">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#4de2c5"/><stop offset="1" stop-color="#7c8cff"/>'
    "</linearGradient></defs>"
    '<rect x="2" y="2" width="44" height="44" rx="12" fill="#0f1730" stroke="url(#g)"/>'
    '<path d="M15 13v13a9 9 0 0 0 18 0V13" fill="none" stroke="url(#g)" '
    'stroke-width="4" stroke-linecap="round"/>'
    '<circle cx="24" cy="35" r="2.5" fill="#4de2c5"/>'
    "</svg>"
)

PAGES: dict[str, tuple[str, str]] = {
    "/": (
        "UQAB demo origin",
        """
        <h1>UQAB lab origin</h1>
        <p class="sub">This page was fetched through the UQAB tunnel: SOCKS5 on
        port 9150, fixed-size cover records, persona timetable.</p>
        <div class="card"><span class="badge">lab simulation</span>
        <p>Every byte above travelled inside padded records. Open the UQAB panel and
        watch <b>real</b> versus <b>cover</b> bytes while you click through the
        pages: the record count and sizes never change with your activity.</p></div>
        <div class="grid">
          <div class="card"><h3>Persona</h3><p class="sub">Record size and slot
          rate follow the persona picked before the session started.</p></div>
          <div class="card"><h3>Floor rate</h3><p class="sub">Even in an idle
          period the scheduler keeps sending, so silence is never a signal.</p></div>
          <div class="card"><h3>Fail closed</h3><p class="sub">Stop the bridge and
          pages stop loading; nothing falls back to a direct connection.</p></div>
        </div>
        """,
    ),
    "/news": (
        "News",
        """
        <h1>Lab news</h1><p class="sub">Static copy, generated locally.</p>
        <div class="card"><table><tr><th>Date</th><th>Item</th></tr>
        <tr><td>W1</td><td>Lab baseline: Tor bootstrapped through the bridge.</td></tr>
        <tr><td>W3</td><td>Tor carried over <code>uqab</code>; a page loaded.</td></tr>
        <tr><td>W6</td><td>Both directions shaped; floor rate held.</td></tr>
        <tr><td>W10</td><td>Feature freeze; app and installer.</td></tr></table></div>
        """,
    ),
    "/docs": (
        "Docs",
        """
        <h1>Documentation</h1>
        <div class="card"><h3>Frame format</h3>
        <p><code>Type 1 B | Length 2 B | Payload</code> with DATA, PADDING, PING and
        CLOSE. Frames over 16,384 B or unknown types close the connection.</p></div>
        <div class="card"><h3>Cover record</h3><p>One fixed-size record per slot:
        DATA frames first, then one PADDING frame that fills the rest.</p></div>
        <div class="card"><h3>Session start</h3><p>The first record carries
        <code>{"v":1,"persona":"...","sha256":"...","seed":...}</code> in a PING
        frame; both ends derive the same timetable from the seed.</p></div>
        """,
    ),
    "/gallery": (
        "Gallery",
        """
        <h1>Gallery</h1><p class="sub">Generated gradients, one HTTP request each.</p>
        <div class="gallery">
        """ + "".join(
            f'<div class="tile" style="background:linear-gradient({45 + i * 17}deg,'
            f'hsl({170 + i * 24} 70% 45%),hsl({220 + i * 11} 65% 35%))"></div>'
            for i in range(9)
        ) + "</div>",
    ),
    "/forum": (
        "Forum",
        """
        <h1>Forum</h1>
        <div class="card"><b>@owner-a</b><p class="sub">Slot error is 1.8 ms at
        p95 on the lab box.</p></div>
        <div class="card"><b>@owner-b</b><p class="sub">Persona-driven cover beats
        constant cover on F1 in the pilot; waiting for W11 to confirm.</p></div>
        <div class="card"><b>@examiner</b><p class="sub">Show me that the persona
        cannot change while connected.</p></div>
        """,
    ),
    "/shop": (
        "Shop",
        """
        <h1>Shop</h1><p class="sub">Fake products, real requests.</p>
        <div class="grid">
        """ + "".join(
            f'<div class="card"><h3>Item {i + 1}</h3><p class="sub">{price} PKR</p>'
            f'<p><span class="badge">in stock</span></p></div>'
            for i, price in enumerate([1200, 3400, 750, 5600, 990, 2100])
        ) + "</div>",
    ),
    "/blog": (
        "Blog",
        """
        <h1>Field notes</h1>
        <div class="card"><h3>Why fixed-size records</h3><p class="sub">Variable
        sizes leak activity no matter how carefully the timing is shaped. One size
        per period, then padding, and the size channel goes quiet.</p></div>
        <div class="card"><h3>Why a stats file</h3><p class="sub">The UI may read
        counters; it must never write anything the scheduler can see. One
        direction only.</p></div>
        """,
    ),
    "/notes": (
        "Notes",
        """
        <h1>Reading notes</h1>
        <div class="card"><p class="sub">Long-form text makes the browser issue
        several requests, which is exactly what the classifier needs in order to
        separate ACTIVE from IDLE windows.</p>
        """ + "<p class='sub'>" + ("The observer sees only record lengths, timing and direction. "
        * 12) + "</p></div>",
    ),
    "/media": (
        "Media",
        """
        <h1>Media</h1><p class="sub">A page with a larger body to move real bytes.</p>
        <div class="card"><pre style="white-space:pre-wrap;color:#93a0c4">""" + "\n".join(
            f"frame {i:03d} | type DATA | len {64 + (i * 37) % 900}" for i in range(40)
        ) + "</pre></div>",
    ),
    "/about": (
        "About",
        """
        <h1>About this origin</h1>
        <p class="sub">Served from 127.0.0.1 by the lab stack. The mock ORPort
        refuses every host except <code>*.uqab</code>, so lab mode cannot be used
        as an open proxy.</p>
        <div class="card"><table>
        <tr><th>Component</th><th>Role</th></tr>
        <tr><td>mock Tor</td><td>SOCKS5 front end + PT client supervision</td></tr>
        <tr><td>PT server</td><td>Tunnel endpoint, ORPort forwarder</td></tr>
        <tr><td>mock ORPort</td><td>Dials the demo origin only</td></tr>
        </table></div>
        """,
    ),
}


def render(path: str) -> tuple[int, bytes]:
    """Return (status, body) for a demo path."""
    if path == "/favicon.ico":
        return HTTPStatus.NO_CONTENT, b""
    title, body = PAGES.get(path, PAGES["/"])
    if path not in PAGES and path != "/":
        title, body = "Not found", "<h1>404</h1><p class='sub'>No such demo page.</p>"
        status = HTTPStatus.NOT_FOUND
    else:
        status = HTTPStatus.OK
    nav = "".join(
        f'<a href="{href}">{html.escape(name.title().replace("/", "") or "Home")}</a>'
        for href, (name, _) in PAGES.items()
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)} - UQAB</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{STYLE}</style></head>
<body>
<header>{LOGO}<b>UQAB</b><span class="badge">demo origin</span>
<nav><a href="/">Home</a>{nav}</nav></header>
<main>{body}</main>
<footer>UQAB lab simulation - local content only. This page travelled through the
persona-shaped tunnel.</footer>
</body></html>"""
    return status, document.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "UQABDemoOrigin/1.0"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        status, body = render(path)
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return None


class DemoOrigin:
    """Threaded demo site on loopback."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> int:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="uqab-demo-origin", daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(description="UQAB demo origin (lab only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args(argv)
    origin = DemoOrigin(args.host, args.port)
    origin.start()
    print(f"demo origin on http://{args.host}:{origin.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        origin.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
