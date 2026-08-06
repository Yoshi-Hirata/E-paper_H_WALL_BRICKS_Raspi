"""Minimal sd_notify: tell systemd we are alive.

Implemented directly against NOTIFY_SOCKET rather than pulling in
python-systemd, which is a compiled dependency for three lines of
datagram. Everything is a no-op when the variable is absent, so running
the UI by hand behaves exactly as before.
"""

import os
import socket


def sd_notify(state: str) -> bool:
    """Send one status datagram to systemd. False if not running under it."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):          # abstract namespace
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
        return True
    except OSError:
        return False


def ready() -> bool:
    """Startup finished - required for Type=notify units."""
    return sd_notify("READY=1")


def alive() -> bool:
    """Pet the watchdog. Must arrive more often than WatchdogSec."""
    return sd_notify("WATCHDOG=1")
