"""The dashboard: a JSON API plus one static page. Reachable over Tailscale (ufw keeps the LAN out)."""

import ipaddress
import json
from http.cookies import CookieError, SimpleCookie
import logging
import mimetypes
import select
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import auth, config, notify, services, store, system, watch

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "static"


def _host_allowed(host: str) -> bool:
    name = host.lower()
    if name.startswith("["):        # [::1]:9000
        name = name[1:].split("]")[0]
    elif name.count(":") == 1:      # pi.local:9000
        name = name.split(":")[0]
    # Only names that point at this machine, so a hostile web page can't reach the API through DNS rebinding.
    if name in ("localhost", config.HOSTNAME.lower(), f"{config.HOSTNAME.lower()}.local", *config.ALLOWED_HOSTS):
        return True
    if name.endswith(".ts.net"):  # Tailscale MagicDNS names
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _timeline(since):
    """Per service, the last 24 h as [[start, end, state], ...]: one stretch per run of equal minute samples.
    Gaps with no samples (pi-dash or the machine was off) are left out."""
    out: dict[str, list] = {}
    rows = store.samples(since)
    for i, row in enumerate(rows):
        t = max(row["t"], since)
        nxt = rows[i + 1]["t"] if i + 1 < len(rows) else None
        end = min(nxt, row["t"] + 2 * config.SAMPLE_EVERY) if nxt else row["t"] + config.SAMPLE_EVERY
        for name, st in row["s"].items():
            segs = out.setdefault(name, [])
            if segs and segs[-1][2] == st and t - segs[-1][1] < 1:
                segs[-1][1] = end
            else:
                segs.append([t, end, st])
    return out


def state():
    now = time.time()
    since = now - 86400
    timeline = _timeline(since)
    svcs = []
    for svc in watch.services_list():
        s = services.status(svc)
        s["timeline"] = timeline.get(svc.name, [])
        s["incidents"] = sum(1 for seg in s["timeline"] if seg[2] in ("down", "unhealthy", "missing"))
        s["uptime24"] = watch.uptime_pct(svc.name, since)
        svcs.append(s)
    return {"now": now, "hostname": config.HOSTNAME, "pi": system.snapshot(), "services": svcs,
            "events": store.events(80), "history": _history(now),
            "muted_until": store.load_state().get("muted_until", 0), "auth": bool(config.PASSWORD_HASH)}


def _history(now, window=6 * 3600, step=300):
    """The Pi's CPU / temperature / memory for the last 6 h: 5-minute averages, plus real peaks."""
    since = now - window
    n = int(window / step)
    sums = {k: [0.0] * n for k in ("cpu", "temp", "mem")}
    counts = [0] * n
    peaks = {k: None for k in sums}
    for row in store.samples(since):
        p = row.get("p")
        if not p:
            continue
        i = min(n - 1, int((row["t"] - since) / step))
        counts[i] += 1
        for k in sums:
            v = p.get(k) or 0
            sums[k][i] += v
            peaks[k] = v if peaks[k] is None else max(peaks[k], v)
    out = {k: [round(v / c, 1) if c else None for v, c in zip(vals, counts)] for k, vals in sums.items()}
    out["peak"] = peaks
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "pi-dash"

    def log_message(self, fmt, *args):  # keep the journal quiet
        pass

    def _json(self, body, code=200, headers=()):
        data = json.dumps(body).encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, write=False) -> bool:
        if not _host_allowed(self.headers.get("Host", "")):
            self._json({"error": "unknown host"}, 403)
            return False
        if write and self.headers.get("X-Pi-Dash") != "1":  # custom header = no cross-site form posts
            self._json({"error": "missing header"}, 403)
            return False
        return True

    # ---- password (only when one is set in the config) ----

    def _direct_local(self) -> bool:
        """A request from this machine itself (e.g. curl on the server), not one relayed by a proxy."""
        return ipaddress.ip_address(self.client_address[0]).is_loopback and not self.headers.get("X-Forwarded-For")

    def _addr(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded and ipaddress.ip_address(self.client_address[0]).is_loopback:  # behind tailscale serve
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return cookie[auth.COOKIE].value if auth.COOKIE in cookie else None

    def _authed(self) -> bool:
        return not config.PASSWORD_HASH or self._direct_local() or auth.valid_session(self._token())

    def _cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return ("Set-Cookie", f"{auth.COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}")

    def _login(self):
        addr = self._addr()
        if auth.locked_out(addr):
            return self._json({"error": "Too many tries. Wait a few minutes."}, 429)
        if config.PASSWORD_HASH and auth.verify_password(str(self._body().get("password", "")), config.PASSWORD_HASH):
            auth.clear_failures(addr)
            return self._json({"ok": True}, headers=[self._cookie(auth.new_session(), auth.SESSION_DAYS * 86400)])
        auth.record_failure(addr)
        time.sleep(0.5)
        return self._json({"error": "Wrong password."}, 401)

    def _service(self, name):
        return next((s for s in watch.services_list() if s.name == name), None)

    def do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if parts[:1] == ["static"] and len(parts) == 2:
                return self._file(STATIC / parts[1])
            if not self._authed():
                if not parts:
                    return self._file(STATIC / "login.html")
                return self._json({"error": "sign in first"}, 401)
            if not parts:
                return self._file(STATIC / "index.html")
            if parts == ["api", "state"]:
                return self._json(state())
            if parts[:2] == ["api", "jobs"] and len(parts) == 3:
                job = services.jobs.get(parts[2])
                if not job:
                    return self._json({"error": "no such job"}, 404)
                start = int(parse_qs(url.query).get("from", ["0"])[0])
                return self._json({**job, "lines": job["lines"][start:], "total": len(job["lines"])})
            if parts[:2] == ["api", "services"] and len(parts) == 4 and parts[3] == "logs":
                svc = self._service(parts[2])
                return self._logs(svc) if svc else self._json({"error": "no such service"}, 404)
        except BrokenPipeError:
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard(write=True):
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts == ["api", "login"]:
            return self._login()
        if parts == ["api", "logout"]:
            auth.end_session(self._token())
            return self._json({"ok": True}, headers=[self._cookie("", 0)])
        if not self._authed():
            return self._json({"error": "sign in first"}, 401)
        if parts[:2] == ["api", "services"] and len(parts) == 4:
            svc, action = self._service(parts[2]), parts[3]
            if not svc:
                return self._json({"error": "no such service"}, 404)
            if action == "auto":
                on = bool(self._body().get("on"))
                off = set(store.load_state().get("auto_update_off", []))
                off.discard(svc.name) if on else off.add(svc.name)
                store.update_state(auto_update_off=sorted(off))
                notify.alert("info", f"Auto-update {'on' if on else 'off'} for {svc.name}", service=svc.name, discord=False)
                return self._json({"auto_update": on})
            if services.running_job(svc):
                return self._json({"error": f"{svc.name} is busy ({services.running_job(svc)})"}, 409)
            if action not in ("start", "stop", "restart", "update") or (action == "update" and not svc.update):
                return self._json({"error": "unknown action"}, 400)
            return self._json(services.start_job(svc, action))
        if parts == ["api", "mute"]:
            hours = float(self._body().get("hours", 0))
            until = time.time() + hours * 3600 if hours > 0 else 0
            store.update_state(muted_until=until)
            if until:
                notify.alert("info", f"Alerts muted for {hours:g} h", "Problems still show here; Discord stays quiet.", discord=False)
            else:
                notify.alert("info", "Alerts back on", discord=False)
            return self._json({"muted_until": until})
        if parts[:2] == ["api", "pi"] and len(parts) == 3 and parts[2] in ("reboot", "poweroff"):
            store.update_state(planned={"action": parts[2], "t": time.time()})
            notify.alert("info", "Restarting the Pi from the dashboard" if parts[2] == "reboot"
                         else "Shutting the Pi down from the dashboard", discord=False)
            subprocess.Popen(["sudo", "-n", "systemctl", parts[2]], env=services.ENV)
            return self._json({"ok": True})
        if parts == ["api", "test-alert"]:
            notify.alert("info", "Test alert from the dashboard", "If you can read this, alerts reach Discord.")
            return self._json({"ok": True})
        if parts == ["api", "digest"]:
            watch.send_digest()
            return self._json({"ok": True})
        if parts == ["api", "reload"]:
            watch.reload_services()
            return self._json({"ok": True, "services": [s.name for s in watch.services_list()]})
        self._json({"error": "not found"}, 404)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if 0 < n < 65536 else {}
        except ValueError:
            return {}

    def _file(self, path: Path):
        path = path.resolve()
        if STATIC not in path.parents or not path.is_file():
            return self._json({"error": "not found"}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(path.name)[0] or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _logs(self, svc):
        """Server-sent events: the last 300 lines, then new ones as they arrive."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        p = subprocess.Popen(services.log_command(svc, 300, follow=True), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=services.ENV)
        try:
            buf = b""
            while True:
                ready, _, _ = select.select([p.stdout], [], [], 15)
                if not ready:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                chunk = p.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                out = []
                for line in lines:
                    try:
                        out.append(b"data: " + json.dumps(services.format_entry(json.loads(line))).encode() + b"\n\n")
                    except ValueError:
                        continue
                if out:
                    self.wfile.write(b"".join(out))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            p.kill()
            p.wait()


def serve():
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    log.info("pi-dash on http://%s:%d", config.HOST, config.PORT)
    httpd.serve_forever()
