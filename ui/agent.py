"""The unit's remote agent: a small HTTP server for the show PC.

Runs inside the UI process, because that is where the serial port lives
(ui/remote.py explains the two-step cue). Standard library only - the
units run Python 3.9 and nothing may be added to their requirements for
a show.

    GET  /status     who am I, what am I doing, which boards answer -
                     and the clock, for the PC's offset measurement
    GET  /clock      the clock alone (smallest, fastest answer)
    POST /prepare    {"cue", "label", "dev_type", "boards": {"1": hex64, ...}}
    POST /fire       {"cue", "at"}       at = this unit's monotonic seconds
    POST /cancel     forget the fire time
    POST /standby    white out the panels, keep the unit under remote
    POST /release    back to the unit's own menu

    POST /show/load    the unit's whole show file (conductor/showfile.py)
    POST /show/preset  put the first cue's picture up, before the start
    POST /show/run     {"t0", "show"}  second 0 of the show, in this
                       unit's monotonic clock - also RESUME and NEXT,
                       which are only a moved T0 (ui/showplay.py)
    POST /show/hold    stop scheduling; POST /show/stop ends the run

The PC polls; the unit never calls out. A unit that walks out of Wi-Fi
range simply stops answering for a while, and nothing here minds.

Trust: the agent only changes what the panels show - no reboot, no
pull, no firmware. On the show's own router that is enough; a shared
network can set a token (--remote-token), which every request must then
carry as X-Show-Token.

Clock: time.monotonic(), stamped as late as possible before the answer
is written. The PC takes the round trip's midpoint as "when the unit
said so" (the NTP idea), and the smallest round trip of a few tries
bounds the error to a few milliseconds on a quiet WLAN.
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .remote import DEV_NUMBER_BRAND, RemoteError, RemoteSession

DEFAULT_PORT = 8787
MAX_BODY = 4 * 1024 * 1024      # a show: cues x boards x 2 x 128 hex chars
API_VERSION = 1


def _clock() -> dict:
    return {"mono": time.monotonic(), "wall": time.time()}


class _Handler(BaseHTTPRequestHandler):
    agent: "Agent" = None              # bound per server in Agent.start()
    server_version = "epaper-agent"
    protocol_version = "HTTP/1.1"      # keep-alive: the PC polls every 2 s
    # One TCP segment per answer, sent at once. Unbuffered (the default),
    # the headers and the body leave as two small writes, and Nagle on
    # this side waits for the PC's delayed ACK before sending the second:
    # measured 2026-09-21 as a constant ~60 ms round trip on a 3 ms WLAN
    # - all of it on the way back, which is exactly the lopsidedness the
    # clock measurement cannot see (it cost ~30 ms of offset error).
    wbufsize = 64 * 1024
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):  # the journal is for the panels
        pass

    def _answer(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        token = self.agent.token
        if not token:
            return True
        given = self.headers.get("X-Show-Token", "")
        if hmac.compare_digest(given.encode(), token.encode()):
            return True
        self._answer(401, {"error": "bad or missing X-Show-Token"})
        return False

    def do_GET(self):
        if not self._allowed():
            return
        if self.path == "/clock":
            return self._answer(200, _clock())
        if self.path == "/status":
            payload = self.agent.status()
            payload["clock"] = _clock()         # last thing before the wire
            return self._answer(200, payload)
        self._answer(404, {"error": "not found"})

    def do_POST(self):
        # Take the body off the wire before answering anything, refusals
        # included: bytes left unread on a kept-alive connection are read
        # as the next request, and closing over them resets the socket
        # before the client has seen the answer.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            return self._answer(413, {"error": "request too large"})
        raw = self.rfile.read(length)
        if not self._allowed():
            return
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise RemoteError("the body must be a JSON object")
            session = self.agent.session
            player = self.agent.player
            if self.path.startswith("/show/"):
                if player is None:
                    raise RemoteError("this unit has no show player")
                if self.path == "/show/load":
                    player.load(body)
                elif self.path == "/show/preset":
                    player.preset()
                elif self.path == "/show/run":
                    player.run(float(body["t0"]), body.get("show"))
                elif self.path == "/show/hold":
                    player.hold()
                elif self.path == "/show/stop":
                    player.stop()
                else:
                    return self._answer(404, {"error": "not found"})
            elif (player is not None and player.running
                  and self.path in ("/prepare", "/fire", "/standby")):
                raise RemoteError("a show is running - stop it first")
            elif self.path == "/prepare":
                boards = {int(address): bytes.fromhex(array)
                          for address, array in body["boards"].items()}
                session.prepare(body["cue"], boards,
                                int(body.get("dev_type", DEV_NUMBER_BRAND)),
                                str(body.get("label", ""))[:40])
            elif self.path == "/fire":
                session.fire(body["cue"], float(body["at"]))
            elif self.path == "/cancel":
                session.cancel()
            elif self.path == "/standby":
                session.standby()
            elif self.path == "/release":
                session.release()
            else:
                return self._answer(404, {"error": "not found"})
        except RemoteError as exc:
            return self._answer(409, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            return self._answer(400, {"error": f"bad request: {exc}"})
        payload = self.agent.status()
        payload["clock"] = _clock()
        self._answer(200, payload)


class Agent:
    def __init__(self, session: RemoteSession, port: int = DEFAULT_PORT,
                 token: "str | None" = None, host: str = "0.0.0.0",
                 commit: str = "?", name: "str | None" = None, player=None):
        self.session = session
        self.player = player
        self.port = port
        self.token = token or None
        self.bind = host
        self.commit = commit
        self.name = name or socket.gethostname()
        self._server: "ThreadingHTTPServer | None" = None
        self._started = time.monotonic()

    def status(self) -> dict:
        payload = self.session.status()
        payload.update({"api": API_VERSION, "host": self.name,
                        "commit": self.commit,
                        "uptime_s": round(time.monotonic() - self._started),
                        "log": self.session.runner.recent(6),
                        "show": (self.player.status() if self.player
                                 else None)})
        return payload

    def start(self) -> int:
        """Serve in a daemon thread; returns the port actually bound."""
        handler = type("BoundHandler", (_Handler,), {"agent": self})
        self._server = ThreadingHTTPServer((self.bind, self.port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
