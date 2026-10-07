"""Unit tests for the parts of pi-dash that don't need a Pi: python3 -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

# Point pi-dash at a throwaway config and state folder before anything imports it.
_tmp = tempfile.TemporaryDirectory()
_config = Path(_tmp.name) / "config.toml"
_config.write_text("""
[server]
port = 9123
allowed_hosts = ["dash.example.org"]
[services]
registry = "%s/services.json"
extra_path = ["~/tools/bin"]
[discord]
webhook_url = "https://discord.com/api/webhooks/1/abc"
ping_user_id = "42"
""" % _tmp.name)
os.environ["PIDASH_CONFIG"] = str(_config)
os.environ["STATE_DIRECTORY"] = str(Path(_tmp.name) / "state")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pidash import auth, config, notify, passwd, services, store, system, watch, web  # noqa: E402


class Config(unittest.TestCase):
    def test_reads_the_file(self):
        self.assertEqual(config.PORT, 9123)
        self.assertEqual(config.PING_USER, "42")
        self.assertTrue(config.WEBHOOK_URL.startswith("https://discord.com/"))
        self.assertEqual(config.EXTRA_PATH, [str(Path("~/tools/bin").expanduser())])

    def test_defaults_when_unset(self):
        self.assertEqual(config.DIGEST_HOUR, 9)
        self.assertEqual(config.DISK_WARN, 80)

    def test_example_config_is_valid_toml(self):
        import tomllib
        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        with example.open("rb") as f:
            self.assertEqual(tomllib.load(f)["server"]["port"], 9000)

    def test_example_registry_loads(self):
        example = Path(__file__).resolve().parent.parent / "examples" / "services.example.json"
        Path(config.REGISTRY).write_text(example.read_text())
        names = [s.name for s in config.load_services()]
        self.assertEqual(names, ["my-bot", "my-app", "my-api"])
        self.assertEqual(config.load_services()[0].repo_path.name, "repo")

    def test_path_includes_extra_path(self):
        self.assertTrue(services.ENV["PATH"].startswith(str(Path("~/tools/bin").expanduser())))


class HostCheck(unittest.TestCase):
    def test_allowed(self):
        for host in ("localhost:9000", f"{config.HOSTNAME}:9000", f"{config.HOSTNAME}.local",
                     "pi.tail1234.ts.net:9443", "10.0.0.5:9000", "[::1]:9000", "dash.example.org"):
            self.assertTrue(web._host_allowed(host), host)

    def test_rejected(self):
        for host in ("evil.example.com", "evil.example.com:9000", "ts.net.evil.com", ""):
            self.assertFalse(web._host_allowed(host), host)


class History(unittest.TestCase):
    def setUp(self):
        for f in (store.SAMPLES, store.EVENTS, store.STATE):
            f.unlink(missing_ok=True)

    def _samples(self, rows):
        with store.SAMPLES.open("w") as f:
            for t, states in rows:
                f.write(json.dumps({"t": t, "s": states}) + "\n")

    def test_timeline_merges_runs_and_keeps_blips(self):
        t0 = time.time() - 600
        self._samples([(t0 + 60 * i, {"a": "down" if i == 3 else "up"}) for i in range(6)])
        segs = web._timeline(t0 - 1)["a"]
        self.assertEqual([s[2] for s in segs], ["up", "down", "up"])
        self.assertEqual(segs[1][1] - segs[1][0], 60)  # the blip lasts exactly its one minute
        self.assertEqual(segs[0][1], segs[1][0])      # no gaps between stretches

    def test_timeline_leaves_gaps_when_nothing_was_recorded(self):
        t0 = time.time() - 7200
        self._samples([(t0, {"a": "up"}), (t0 + 60, {"a": "up"}), (t0 + 3600, {"a": "up"})])
        segs = web._timeline(t0 - 1)["a"]
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0][1], t0 + 180)  # stops two samples' worth after the last one

    def test_history_averages_and_peaks(self):
        store.add_sample({}, {"cpu": 10, "temp": 40, "mem": 30})
        store.add_sample({}, {"cpu": 30, "temp": 60, "mem": 50})
        h = web._history(time.time() + 1)
        self.assertEqual(h["cpu"][-1], 20)
        self.assertEqual(h["peak"]["temp"], 60)
        self.assertIsNone(h["cpu"][0])

    def test_events_newest_first_and_capped(self):
        for i in range(5):
            store.add_event("info", f"e{i}", log_lines=[f"line {i}"])
        rows = store.events(3)
        self.assertEqual([r["title"] for r in rows], ["e4", "e3", "e2"])
        self.assertEqual(rows[0]["log"], ["line 4"])

    def test_prune_drops_old_rows(self):
        store.EVENTS.write_text(json.dumps({"t": 1, "level": "info", "title": "old"}) + "\n")
        store.add_event("info", "new")
        store.prune()
        self.assertEqual([r["title"] for r in store.events()], ["new"])

    def test_state_round_trip(self):
        store.update_state(stopped=["a"])
        store.update_state(muted_until=5)
        self.assertEqual(store.load_state(), {"stopped": ["a"], "muted_until": 5})


class Logs(unittest.TestCase):
    def test_error_lines(self):
        hits = ["Traceback (most recent call last):", "2026-10-07 01:00:00 ERROR mybot: boom",
                "TypeError: x is undefined", "UnhandledPromiseRejection: nope", "panic: runtime error", "FATAL: db"]
        misses = ["INFO error handling enabled", "0 errors, 2 warnings", "errorCount=0", "GET /error 200"]
        for line in hits:
            self.assertTrue(services.ERROR_RE.search(line), line)
        for line in misses:
            self.assertFalse(services.ERROR_RE.search(line), line)

    def test_format_entry_decodes_bytes(self):
        entry = {"MESSAGE": list("héllo".encode()), "__REALTIME_TIMESTAMP": "1700000000000000", "PRIORITY": "3"}
        row = services.format_entry(entry)
        self.assertEqual(row["msg"], "héllo")
        self.assertEqual(row["t"], 1700000000)

    def test_docker_times(self):
        self.assertEqual(services._parse_docker_time("2023-11-14T22:13:20.123456789Z"), 1700000000)
        self.assertIsNone(services._parse_docker_time("0001-01-01T00:00:00Z"))
        self.assertIsNone(services._parse_docker_time(""))


class Messages(unittest.TestCase):
    def test_red_alerts_ping(self):
        body = notify.payload(("error", "x crashed", "why", "x", [], ["a", "b"]))
        self.assertEqual(body["content"], "<@42>")
        self.assertEqual(body["allowed_mentions"], {"users": ["42"]})
        self.assertIn("```", body["embeds"][0]["description"])

    def test_other_alerts_never_ping(self):
        for level in ("warn", "ok", "info", "boot", "update", "digest"):
            body = notify.payload((level, "t", "d", None, [], []))
            self.assertNotIn("content", body)
            self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_no_ping_without_a_user(self):
        self.assertNotIn("content", notify.payload(("error", "t", "", None, [], []), ping_user=""))

    def test_long_text_is_cut_to_discord_limits(self):
        embed = notify.payload(("warn", "t" * 400, "d" * 5000, None, [], ["x" * 3000]))["embeds"][0]
        self.assertLessEqual(len(embed["title"]), 256)
        self.assertLessEqual(len(embed["description"]), 4000)


class PiStatus(unittest.TestCase):
    def test_throttle_flags(self):
        value, flags = system.parse_throttled("throttled=0x50005")
        self.assertEqual(value, 0x50005)
        self.assertIn("under-voltage now", flags)
        self.assertIn("under-voltage since boot", flags)
        self.assertEqual(system.parse_throttled("throttled=0x0"), (0, []))
        self.assertEqual(system.parse_throttled(""), (0, []))

    def test_health_check_without_a_temperature_sensor(self):
        real = system.snapshot
        fake = {"disk": {"used": 1, "total": 10, "free": 9}, "temp": None, "mem": {"available": 5, "total": 10},
                "power": {"flags": []}}
        system.snapshot = lambda: fake
        try:
            with unittest.mock.patch.object(services, "run", return_value=(0, "")):
                watch._health_once()  # must not raise
        finally:
            system.snapshot = real

    def test_durations(self):
        self.assertEqual(watch._ago(30), "30 s")
        self.assertEqual(watch._ago(600), "10 min")
        self.assertEqual(watch._ago(7300), "2 h 1 min")
        self.assertEqual(watch._ago(3 * 86400), "3 days")


class Password(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = auth.hash_password("correct horse")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn("correct horse", stored)
        self.assertTrue(auth.verify_password("correct horse", stored))
        self.assertFalse(auth.verify_password("wrong", stored))
        self.assertFalse(auth.verify_password("x", "garbage"))
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))  # salted

    def test_sessions(self):
        token = auth.new_session()
        self.assertTrue(auth.valid_session(token))
        self.assertNotIn(token, json.dumps(store.load_state()))  # only a hash is kept
        auth.end_session(token)
        self.assertFalse(auth.valid_session(token))
        self.assertFalse(auth.valid_session(None))

    def test_lockout_after_repeated_failures(self):
        addr = "100.64.0.9"
        for _ in range(auth.MAX_FAILURES):
            self.assertFalse(auth.locked_out(addr))
            auth.record_failure(addr)
        self.assertTrue(auth.locked_out(addr))
        auth.clear_failures(addr)
        self.assertFalse(auth.locked_out(addr))

    def test_passwd_writes_the_hash_into_the_config(self):
        original = config.CONFIG_FILE.read_text()
        try:
            passwd.write_hash("scrypt$abc")
            self.assertIn('password_hash = "scrypt$abc"', config.CONFIG_FILE.read_text())
            passwd.write_hash("")
            text = config.CONFIG_FILE.read_text()
            self.assertIn('password_hash = ""', text)
            self.assertEqual(text.count("password_hash"), 1)
        finally:
            config.CONFIG_FILE.write_text(original)


class HttpWithPassword(unittest.TestCase):
    """A real server on a free port. Requests carry X-Forwarded-For, like ones relayed by tailscale serve,
    so they aren't treated as coming from the machine itself."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        cls.saved = config.PASSWORD_HASH
        config.PASSWORD_HASH = auth.hash_password("let me in")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        config.PASSWORD_HASH = cls.saved

    def request(self, path, method="GET", body=None, cookie=None, relayed=True):
        import urllib.error
        import urllib.request
        headers = {"X-Pi-Dash": "1", "Content-Type": "application/json"}
        if relayed:
            headers["X-Forwarded-For"] = "100.64.0.5"
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def test_signed_out_sees_only_the_sign_in_page(self):
        code, html, _ = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn('id="login"', html)
        self.assertEqual(self.request("/api/jobs/1")[0], 401)
        self.assertEqual(self.request("/api/reload", "POST", {})[0], 401)
        self.assertEqual(self.request("/static/app.css")[0], 200)

    def test_wrong_then_right_password(self):
        auth.clear_failures("100.64.0.5")
        self.assertEqual(self.request("/api/login", "POST", {"password": "nope"})[0], 401)
        code, _, headers = self.request("/api/login", "POST", {"password": "let me in"})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        session = cookie.split(";")[0]
        self.assertEqual(self.request("/api/jobs/1", cookie=session)[0], 404)  # signed in: a normal "no such job"
        code, html, _ = self.request("/", cookie=session)
        self.assertIn('id="services"', html)
        self.request("/api/logout", "POST", {}, cookie=session)
        self.assertEqual(self.request("/api/jobs/1", cookie=session)[0], 401)

    def test_login_needs_the_custom_header(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base + "/api/login", method="POST", data=b'{"password": "let me in"}',
                                     headers={"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.6"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 403)

    def test_curl_on_the_server_itself_needs_no_password(self):
        self.assertEqual(self.request("/api/jobs/1", relayed=False)[0], 404)


if __name__ == "__main__":
    unittest.main()
