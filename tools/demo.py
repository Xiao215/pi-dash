"""Serve the dashboard with made-up data: for working on the page without a server, and for screenshots.

    python3 tools/demo.py              # http://localhost:9100
    python3 tools/demo.py --trouble    # one service down, a hot CPU, a red alert
"""

import argparse
import json
import math
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

STATIC = Path(__file__).resolve().parent.parent / "static"
GB = 1024 ** 3
random.seed(7)


def timeline(now, blips=()):
    """24 h of "up" with short blips: (hours_ago, minutes, state)."""
    segs, t = [], now - 86400
    for hours_ago, minutes, state in sorted(blips, reverse=True):
        a = now - hours_ago * 3600
        segs += [[t, a, "up"], [a, a + minutes * 60, state]]
        t = a + minutes * 60
    if t < now:
        segs.append([t, now, "up"])
    return [s for s in segs if s[1] > s[0]]


def series(base, wave, noise, n=72):
    return [round(max(0, base + wave * math.sin(i / 9) + random.uniform(-noise, noise)), 1) for i in range(n)]


def state(trouble):
    now = time.time()
    cpu, temp, mem = series(14, 9, 5), series(47, 4, 1.2), series(38, 3, 1)
    bot_state = "down" if trouble else "up"
    services = [
        {
            "name": "discord-bot", "kind": "compose", "description": "Chat bot for the group server",
            "state": bot_state, "raw": "exited" if trouble else "running", "since": now - (240 if trouble else 3 * 86400 + 7200),
            "cpu": None if trouble else 0.8, "mem": None if trouble else 118 * 1024 ** 2, "restarts": 3 if trouble else 0,
            "exit_code": 1 if trouble else 0,
            "health": {"ok": not trouble, "ms": 4, "error": "connection refused" if trouble else ""},
            "source": {"kind": "git", "sha": "4f2c9e1", "subject": "Answer in threads when someone replies to the bot",
                       "time": now - 2 * 86400, "behind": 2, "newest": "8d01b7a Add /remind", "dirty": False},
            "auto_update": True, "can_update": True, "busy": None, "problem": "",
            "timeline": timeline(now, [(17.5, 6, "down"), (0.07, 4, "down")] if trouble else [(17.5, 6, "down")]),
            "incidents": 2 if trouble else 1,
            "uptime24": 91.7 if trouble else 99.31,
        },
        {
            "name": "music-server", "kind": "compose", "description": "Streams the music library; public behind Tailscale Funnel",
            "url": "https://example.com", "state": "up", "raw": "running", "since": now - 9 * 3600,
            "cpu": 2.4, "mem": 262 * 1024 ** 2, "restarts": 0, "health": {"ok": True, "ms": 3, "error": ""},
            "source": {"kind": "image", "image": "ghcr.io/you/music-server:latest", "sha": "865767f",
                       "time": now - 9 * 3600 - 600, "behind": 0, "dirty": False},
            "auto_update": True, "can_update": True, "busy": None, "problem": "",
            "timeline": timeline(now, [(9, 2, "starting")]), "incidents": 0, "uptime24": 99.9,
        },
        {
            "name": "local-api", "kind": "systemd-user", "description": "Small API the bot calls on this machine",
            "state": "up", "raw": "active", "since": now - 5 * 86400, "cpu": 0.1, "mem": 21 * 1024 ** 2, "restarts": 0,
            "health": {"ok": True, "ms": 2, "error": ""},
            "source": {"kind": "git", "sha": "f51efe0", "subject": "Stream replies as they're written",
                       "time": now - 6 * 86400, "behind": 0, "dirty": False},
            "auto_update": False, "can_update": True, "busy": None, "problem": "", "timeline": timeline(now), "incidents": 0, "uptime24": 100.0,
        },
    ]
    events = [
        {"t": now - 1500, "level": "update", "title": "music-server updated", "service": "music-server",
         "detail": "Now running the image published 25 min ago."},
        {"t": now - 3 * 3600, "level": "ok", "title": "discord-bot is back up", "service": "discord-bot", "detail": ""},
        {"t": now - 3 * 3600 - 120, "level": "error", "title": "discord-bot crashed", "service": "discord-bot",
         "detail": "Exited with code 1. It restarts automatically.",
         "log": ["Traceback (most recent call last):", '  File "/app/bot.py", line 88, in on_message',
                 "    reply = await llm.answer(message)", "TimeoutError: the model took longer than 90 s"]},
        {"t": now - 26 * 3600, "level": "boot", "title": "Pi restarted", "service": None,
         "detail": "Reason: scheduled restart after an automatic update.\nWas down for about 48 s."},
        {"t": now - 2 * 86400, "level": "warn", "title": "Disk 82% full", "service": None,
         "detail": "5.1 GB left on the SD card."},
    ]
    if trouble:
        events.insert(0, {"t": now - 200, "level": "error", "title": "discord-bot is down", "service": "discord-bot",
                          "detail": "It isn't running.", "log": ["discord.errors.LoginFailure: Improper token has been passed."]})
        cpu[-1], temp[-1] = 88, 78
    return {
        "now": now, "hostname": "homelab", "muted_until": 0, "events": events, "services": services,
        "history": {"cpu": cpu, "temp": temp, "mem": mem,
                    "peak": {"cpu": max(cpu) + 12, "temp": max(temp) + 1, "mem": max(mem)}},
        "pi": {
            "model": "Raspberry Pi 4 Model B Rev 1.5", "os": "Debian GNU/Linux 13 (trixie)", "kernel": "6.12",
            "cpu": cpu[-1], "load": [0.42, 0.37, 0.31], "cores": 4, "temp": temp[-1],
            "mem": {"total": 3.7 * GB, "available": 2.3 * GB}, "swap": {"total": 2 * GB, "free": 2 * GB},
            "disk": {"total": 28.7 * GB, "used": 9.4 * GB, "free": 19.3 * GB}, "uptime": 26 * 3600 + 1500,
            "power": {"value": "0x0", "flags": []}, "reboot_required": False,
            "network": {"addresses": {"wlan0": "192.168.1.40"}, "wifi": {"ssid": "home-wifi", "signal": 71},
                        "tailscale": {"state": "Running", "name": "homelab.example.ts.net"}},
        },
    }


LOG = [
    "INFO discord.client: logging in using static token",
    "INFO bot.web: Health server listening on :8080",
    "INFO discord.gateway: Shard ID None has connected to Gateway",
    "INFO bot: Logged in as demo-bot#0001 (3 guilds)",
    "INFO bot: Synced 11 commands",
    "INFO bot.llm: answered in 2.1 s (412 tokens)",
    "WARNING bot.llm: provider slow, retrying once",
    "ERROR bot.llm: request failed: TimeoutError: the model took longer than 90 s",
    "INFO bot.llm: answered in 3.4 s (530 tokens)",
]


class Handler(BaseHTTPRequestHandler):
    trouble = False

    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/static/") and (STATIC / path[8:]).is_file():
            ctype = {"css": "text/css", "js": "text/javascript", "svg": "image/svg+xml", "html": "text/html", "png": "image/png"}.get(path.rsplit(".", 1)[-1], "text/plain")
            return self._send((STATIC / path[8:]).read_bytes(), ctype + "; charset=utf-8")
        if path == "/api/state":
            return self._send(state(self.trouble))
        if path.endswith("/logs"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            now = time.time()
            for i, line in enumerate(LOG * 3):
                row = {"t": now - (len(LOG) * 3 - i) * 37, "msg": line, "error": line.startswith("ERROR"), "p": 6}
                self.wfile.write(b"data: " + json.dumps(row).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(3600)
            return
        if path.startswith("/api/jobs/"):
            return self._send({"id": "1", "lines": ["$ git -C repo pull --ff-only", "Updating 4f2c9e1..8d01b7a",
                                                    "$ docker compose up -d --build", " Container discord-bot Started"],
                               "total": 4, "done": True, "ok": True})
        self._send({"error": "not found"}, code=404)

    def do_POST(self):
        self._send({"id": "1", "ok": True, "muted_until": 0})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--trouble", action="store_true", help="show a service down and a hot CPU")
    args = p.parse_args()
    Handler.trouble = args.trouble
    ThreadingHTTPServer.daemon_threads = True
    print(f"pi-dash demo on http://localhost:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
