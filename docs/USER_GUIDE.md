# UQAB user guide

UQAB is a browser that hides *when* you are doing something, not only *what* you
are doing. Your traffic is carried inside cover records that follow a schedule
(a "persona"), so an observer watching the connection cannot tell a page load from
an idle pause.

This guide is for the person using the app. You do not need Tor, Python or any
configuration file.

## Install

**Windows** — run `UQAB-Setup-<version>.exe`.
It installs for your user only: no administrator prompt, no system-wide changes.
You may choose the folder. A shortcut is added to the Start menu and the desktop,
and UQAB appears in "Apps & features" so you can uninstall it normally.

**Linux** — either

```bash
chmod +x UQAB-<version>-linux-x86_64.AppImage
./UQAB-<version>-linux-x86_64.AppImage
```

or, for a system install with a menu entry:

```bash
sudo apt install ./UQAB-<version>-linux-amd64.deb
```

**Portable (Windows)** — unzip `UQAB-portable-win64.zip` anywhere (a USB stick
works) and double-click `UQAB.exe`.

## First start: the Setup screen

UQAB opens a three-step setup the first time. Nothing is sent anywhere while you
fill it in.

1. **Connection.** Choose how UQAB reaches the network.
   * **Automatic** (recommended) — use Tor if it is installed on this computer,
     otherwise use the built-in **Lab simulation** so the app still works. The screen
     tells you which one will be used and where it found Tor.
   * **Use my Tor** — press *Browse* and pick your `tor` binary (`tor.exe` on
     Windows, usually inside the Tor Browser folder).
   * **Lab simulation** — no Tor at all; a demo stack runs on your machine. It is
     labelled *Lab simulation* in the app at all times.
   * Optionally set the SOCKS port (default 9150) and the default persona, and add
     bridges under *Advanced* if your provider gave you some.
2. **Personas and privacy.** Clearnet learning is **off** unless you opt in later;
   the panel shows its current state and lets you withdraw (which deletes the
   learned profile). You can also tick *Start UQAB when I sign in*.
3. **Ready.** A summary of your choices. *Finish* saves them and restarts the engine.

You can reopen this screen at any time with **Setup: connection & Tor** in the
right-hand panel.

## Everyday use

* Pick a **persona** in the panel. `balanced`, `high` and `low` cover different
  usage styles; `constant` sends a perfectly steady stream (the comparison
  baseline). Personas you generate yourself appear in the same list.
* Press **Start**. The state goes *Connecting* → *Protected*. Only then does the
  address bar unlock — until the tunnel is up, UQAB refuses to load anything, which
  is the point: no traffic ever leaves unprotected.
* Browse. Every tab goes through UQAB's own SOCKS5 listener with remote DNS, and
  WebRTC cannot send UDP outside the tunnel.
* The panel updates once a second: real versus cover traffic, overhead, the
  persona's current period and the session uptime.
* Press **Stop** to end the session. Traffic stops with the tunnel.

Shortcuts: `Ctrl+T` new tab, `Ctrl+W` close tab, `Ctrl+L` address bar, `Ctrl+H`
history, `Ctrl+Tab` next tab.

## If something goes wrong

| What you see | What it means | What to do |
| --- | --- | --- |
| *Blocked* with "the tunnel backend exited" | The engine process died; nothing is being sent | Press Start again; if it repeats, open Setup and switch to Lab simulation to confirm the app itself is fine |
| *Blocked* with "no Tor" | You chose *Use my Tor* but the binary moved | Setup → *Browse* and pick it again, or choose Automatic |
| A page says "Could not load that page" | The destination refused the connection | Retry; while *Connecting*, wait for *Protected* |
| Panel says "the engine has not reported for a few seconds" | The engine is busy or hung | Stop, then Start |
| Nothing happens when you press Start | Port 9150 is taken by another Tor | Setup → change the SOCKS port |

## Privacy facts you can rely on

* UQAB makes no telemetry calls, checks for updates from nowhere and loads no
  remote resources in its own UI.
* The only network traffic is the session you start.
* Clearnet learning is opt-in, deletes the capture it was given, and stores
  aggregate numbers only (no host names, URLs or page text).
* Uninstalling removes the app. Your settings and history live in the app's
  user-data folder and are removed too if you let the uninstaller delete app data;
  the learned profile can be deleted from the panel with *Withdraw & delete*.

## What UQAB is not

It is not an anonymity network on its own. It shapes the traffic of a Tor
connection; Tor provides the anonymity. In **Lab simulation** there is no Tor at
all — the transport, shaping and browser work, but nothing is anonymised. The app
always labels that mode.
