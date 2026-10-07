"""The dashboard: a JSON API plus one static page. Reachable over Tailscale (ufw keeps the LAN out)."""

import hashlib
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

from . import auth, config, notify, scheduled, services, store, system, watch

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "static"


_version = {"key": None, "value": ""}


def static_version() -> str:
    """A short hash of the page's files. The page puts it on its own CSS/JS links and reloads itself when
    /api/state reports a different one, so a tab left open picks up an update of pi-dash."""
    files = sorted(p for p in STATIC.iterdir() if p.is_file())
    key = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if key != _version["key"]:
        h = hashlib.sha256()
        for p in files:
            h.update(p.name.encode() + b"\0" + p.read_bytes())
        _version.update(key=key, value=h.hexdigest()[:12])
    return _version["value"]


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


def _timeline(since, rows=None):
    """Per service, the time since `since` as [[start, end, state], ...]: one stretch per run of equal minute
    samples. Gaps with no samples (pi-dash or the machine was off) are left out."""
    out: dict[str, list] = {}
    rows = store.samples(since) if rows is None else rows
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
    heartbeat = {**watch.heartbeat, "every": config.HEARTBEAT_EVERY} if config.HEARTBEAT_URL else None
    return {"now": now, "hostname": config.HOSTNAME, "pi": system.snapshot(), "services": svcs,
            "jobs": [scheduled.status(j, now) for j in scheduled.jobs_list()],
            "events": store.events(80), "history": _history(now), "heartbeat": heartbeat, "version": static_version(),
            "muted_until": store.load_state().get("muted_until", 0), "auth": bool(config.PASSWORD_HASH)}


def _buckets(rows, since, n, step, get):
    """Averages of get(row) per step-long bucket (None where nothing was recorded), and the highest single value."""
    sums, counts, peak = [0.0] * n, [0] * n, None
    for row in rows:
        v = get(row)
        i = int((row["t"] - since) / step)
        if v is None or i < 0:
            continue
        i = min(n - 1, i)
        sums[i] += v
        counts[i] += 1
        peak = v if peak is None else max(peak, v)
    return [round(s / c, 1) if c else None for s, c in zip(sums, counts)], peak


def _history(now, window=6 * 3600, step=300):
    """The Pi's CPU, temperature, memory and network for the last 6 h: 5-minute averages, plus real peaks,
    and which of those 5 minutes had the internet down."""
    since = now - window
    n = int(window / step)
    rows = store.samples(since)
    out, peaks = {}, {}
    for k in ("cpu", "temp", "mem", "rx", "tx"):
        out[k], peaks[k] = _buckets(rows, since, n, step, lambda r, k=k: (r.get("p") or {}).get(k))
    offline, _ = _buckets(rows, since, n, step, lambda r: None if "p" not in r else 0 if r["p"].get("online", True) else 1)
    out["offline"] = [bool(v) for v in offline]
    out["peak"] = peaks
    return out


RANGES = {"6h": (6 * 3600, 300), "24h": (86400, 900), "7d": (7 * 86400, 3600)}


def service_history(svc, rng):
    """One service over 6 h, 24 h or 7 days: CPU, memory and health-check time, its up/down stretches,
    what happened to it, the code it runs and how it's set up."""
    window, step = RANGES.get(rng, RANGES["24h"])
    now = time.time()
    since = now - window
    n = window // step
    rows = store.samples(since)
    name = svc.name

    def res(i):
        return lambda r: ((r.get("r") or {}).get(name) or (None, None, None))[i]
    series, peaks = {}, {}
    for i, k in enumerate(("cpu", "mem", "ms")):
        series[k], peaks[k] = _buckets(rows, since, n, step, res(i))
    timeline = _timeline(since, rows).get(name, [])
    config_view = {"kind": svc.kind, "container": svc.container or (svc.name if svc.kind == "compose" else ""),
                   "unit": svc.unit, "dir": svc.dir, "repo": svc.repo, "health": svc.health, "url": svc.url,
                   "check": svc.check.get("command", ""), "update": svc.update}
    return {"now": now, "range": rng if rng in RANGES else "24h", "since": since, "step": step, **series, "peak": peaks,
            "timeline": timeline, "uptime": watch.uptime_pct(name, since, rows),
            "incidents": sum(1 for seg in timeline if seg[2] in ("down", "unhealthy", "missing")),
            "events": [e for e in store.events(2000, since) if e.get("service") == name][:60],
            "code": services.code_history(svc), "config": config_view}


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
                    return self._page("login.html")
                return self._json({"error": "sign in first"}, 401)
            if not parts:
                return self._page("index.html")
            if parts == ["api", "state"]:
                return self._json(state())
            if parts[:2] == ["api", "jobs"] and len(parts) == 3:
                job = services.jobs.get(parts[2])
                if not job:
                    return self._json({"error": "no such job"}, 404)
                start = int(parse_qs(url.query).get("from", ["0"])[0])
                return self._json({**job, "lines": job["lines"][start:], "total": len(job["lines"])})
            if parts[:2] == ["api", "services"] and len(parts) == 4 and parts[3] in ("logs", "history"):
                svc = self._service(parts[2])
                if not svc:
                    return self._json({"error": "no such service"}, 404)
                if parts[3] == "history":
                    return self._json(service_history(svc, parse_qs(url.query).get("range", ["24h"])[0]))
                return self._logs(svc)
            if parts[:2] == ["api", "scheduled"] and len(parts) == 4 and parts[3] in ("logs", "output"):
                job = scheduled.get(parts[2])
                if not job:
                    return self._json({"error": "no such job"}, 404)
                if parts[3] == "output":  # what the last ping sent along
                    return self._json({"lines": store.load_state().get("job_logs", {}).get(job.name, [])})
                return self._logs(scheduled.as_service(job)) if job.timer else self._json({"error": "not a timer"}, 400)
        except BrokenPipeError:
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        local_ping = parts[:2] == ["api", "ping"] and self._direct_local()  # cron on the Pi: plain `curl -d`
        if not self._guard(write=not local_ping):
            return
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
        if parts[:2] == ["api", "ping"] and len(parts) in (3, 4):
            job = scheduled.get(parts[2])
            if not job:
                return self._json({"error": f"no job named {parts[2]!r} in {config.REGISTRY}; add it under \"jobs\" "
                                   "and reload"}, 404)
            try:
                scheduled.ping(job, parts[3] if len(parts) == 4 else "", self._text_body())
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True})
        if parts[:2] == ["api", "scheduled"] and len(parts) == 4 and parts[3] == "run":
            job = scheduled.get(parts[2])
            if not job or not job.timer:
                return self._json({"error": "no such timer"}, 404)
            scheduled.run_now(job)
            notify.alert("info", f"Started {job.name} from the dashboard", service=job.name, discord=False)
            return self._json({"ok": True})
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
            return self._json({"ok": True, "services": [s.name for s in watch.services_list()],
                               "jobs": [j.name for j in scheduled.jobs_list()]})
        self._json({"error": "not found"}, 404)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if 0 < n < 65536 else {}
        except ValueError:
            return {}

    def _text_body(self, limit=65536) -> str:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, limit)).decode("utf-8", "replace") if n > 0 else ""

    def _page(self, name):
        """An HTML page, with the current version on its CSS and JS links so browsers never mix old and new."""
        v = static_version()
        html = (STATIC / name).read_text().replace('/static/app.css"', f'/static/app.css?v={v}"') \
            .replace('/static/app.js"', f'/static/app.js?v={v}"')
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
