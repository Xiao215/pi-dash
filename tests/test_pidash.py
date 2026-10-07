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

from pidash import auth, config, notify, passwd, scheduled, services, store, system, watch, web  # noqa: E402


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
        jobs = config.load_jobs()
        self.assertEqual([(j.name, j.every, j.service) for j in jobs],
                         [("backup", 86400, "backup.service"), ("photo-sync", 21600, "")])

    def test_durations_in_the_registry(self):
        self.assertEqual([config.seconds(v) for v in (300, "300", "45s", "30m", "6h", "1d", "1.5h")],
                         [300, 300, 45, 1800, 21600, 86400, 5400])
        job = config.Job(name="backup", timer="backup.timer", every="1d")
        self.assertEqual((job.every, job.grace, job.service), (86400, 8640, "backup.service"))
        self.assertEqual(config.Job(name="x", every="1h").grace, 600)  # at least 10 minutes

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


    def test_sample_cache_follows_appends_and_rewrites(self):
        store.add_sample({"a": "up"})
        self.assertEqual(len(store.samples(time.time() - 60)), 1)
        store.add_sample({"a": "down"})
        self.assertEqual([r["s"]["a"] for r in store.samples(time.time() - 60)], ["up", "down"])
        self._samples([(time.time(), {"b": "up"})])  # rewritten, like prune() does
        self.assertEqual([list(r["s"]) for r in store.samples(time.time() - 60)], [["b"]])
        store.SAMPLES.unlink()
        self.assertEqual(store.samples(time.time() - 60), [])

    def test_history_includes_network_and_outages(self):
        store.add_sample({}, {"cpu": 1, "temp": None, "mem": 2, "rx": 1000, "tx": 10, "online": False})
        h = web._history(time.time() + 1)
        self.assertEqual(h["rx"][-1], 1000)
        self.assertTrue(h["offline"][-1])
        self.assertFalse(h["offline"][0])
        self.assertIsNone(h["temp"][-1])  # no sensor: no data, not 0 °C

    def test_service_history(self):
        svc = config.Service(name="a", kind="compose", health="http://x")
        now = time.time()
        with store.SAMPLES.open("w") as f:
            for i in range(10):
                f.write(json.dumps({"t": now - 600 + 60 * i, "s": {"a": "up"}, "r": {"a": [1.0 + i, 100 + i, 5]}}) + "\n")
        store.add_event("error", "a crashed", service="a")
        store.add_event("error", "b crashed", service="b")
        h = web.service_history(svc, "6h")
        self.assertEqual(len(h["cpu"]), 72)
        self.assertEqual(h["peak"]["mem"], 109)
        self.assertEqual(h["uptime"], 100)
        self.assertEqual([e["title"] for e in h["events"]], ["a crashed"])
        self.assertEqual(h["config"]["container"], "a")
        self.assertEqual(web.service_history(svc, "nonsense")["range"], "24h")


class MemoryGrowth(unittest.TestCase):
    def rows(self, mb_at):
        t0 = time.time() - 6 * 3600
        return [{"t": t0 + 60 * i, "s": {}, "r": {"a": [1, mb_at(i), None]}} for i in range(360)]

    def test_steady_growth_is_noticed(self):
        self.assertEqual(watch.memory_growth(self.rows(lambda i: 200 + i), "a"), (229.5, 529.5))

    def test_flat_noisy_or_restarted_is_not(self):
        self.assertIsNone(watch.memory_growth(self.rows(lambda i: 300 + (i % 7)), "a"))
        self.assertIsNone(watch.memory_growth(self.rows(lambda i: 200 + (i % 180) * 2), "a"))  # restarted halfway
        self.assertIsNone(watch.memory_growth(self.rows(lambda i: 20 + i / 10), "a"))  # grew, but only 36 MB
        self.assertIsNone(watch.memory_growth(self.rows(lambda i: 200 + i)[:100], "a"))  # not enough history


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


class PruneImages(unittest.TestCase):
    OUT = "Deleted Images:\ndeleted: sha256:abc\ndeleted: sha256:def\n\nTotal reclaimed space: 1.214GB\n"
    CACHE = "ID\t\tRECLAIMABLE\tSIZE\t\tLAST ACCESSED\nab12\ttrue\t300MB\t9 days ago\nTotal:\t386.5MB\n"

    def fake(self, cmd, timeout=None):
        return 0, self.OUT if cmd[1] == "image" else self.CACHE

    def setUp(self):
        services.jobs.clear()

    def test_reports_what_was_freed(self):
        job = {"lines": [], "done": False, "action": "update"}
        with unittest.mock.patch.object(services, "run", side_effect=self.fake):
            self.assertEqual(services.prune_images(job), "1.6 GB")  # images + week-old build cache
        self.assertEqual(job["lines"], ["$ docker image prune -f", "Total reclaimed space: 1.214GB",
                                        "$ docker builder prune -f --filter until=168h", "Total:\t386.5MB"])
        with unittest.mock.patch.object(services, "run", return_value=(0, "Total reclaimed space: 0B\nTotal:\t0B\n")):
            self.assertEqual(services.prune_images(), "")
        with unittest.mock.patch.object(services, "run", return_value=(1, "Cannot connect to the Docker daemon")):
            self.assertEqual(services.prune_images(), "")

    def test_waits_while_another_update_runs(self):
        services.jobs["1"] = {"done": False, "action": "update"}
        with unittest.mock.patch.object(services, "run", return_value=(0, self.OUT)) as run:
            self.assertEqual(services.prune_images(), "")
        run.assert_not_called()

    def test_can_be_switched_off(self):
        with unittest.mock.patch.object(config, "PRUNE_IMAGES", False), \
                unittest.mock.patch.object(services, "run", return_value=(0, self.OUT)) as run:
            self.assertEqual(services.prune_images(), "")
        run.assert_not_called()


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

    def test_network_counters_skip_virtual_interfaces(self):
        text = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0
 wlan0: 1000 10 0 0 0 0 0 0 200 10 0 0 0 0 0 0
  eth0: 30 1 0 0 0 0 0 0 4 1 0 0 0 0 0 0
tailscale0: 900 9 0 0 0 0 0 0 100 1 0 0 0 0 0 0
docker0: 7 1 0 0 0 0 0 0 7 1 0 0 0 0 0 0
vethab12: 7 1 0 0 0 0 0 0 7 1 0 0 0 0 0 0
"""
        self.assertEqual(system.parse_net_dev(text), (1030, 204))

    def test_pending_os_updates(self):
        out = """Listing...
openssl/stable-security 3.0.17-1~deb12u3 arm64 [upgradable from: 3.0.17-1~deb12u2]
tzdata/stable-updates 2025b-0+deb12u2 all [upgradable from: 2025b-0+deb12u1]
raspi-firmware/stable 1:1.20250915-1 arm64 [upgradable from: 1:1.20250430-1]
"""
        pkgs = system.parse_upgradable(out)
        self.assertEqual([p["name"] for p in pkgs], ["openssl", "tzdata", "raspi-firmware"])
        self.assertEqual([p["security"] for p in pkgs], [True, False, False])
        self.assertEqual(system.parse_upgradable(""), [])

    def test_durations(self):
        self.assertEqual(watch._ago(30), "30 s")
        self.assertEqual(watch._ago(600), "10 min")
        self.assertEqual(watch._ago(7300), "2 h 1 min")
        self.assertEqual(watch._ago(3 * 86400), "3 days")


class ScheduledJobs(unittest.TestCase):
    def setUp(self):
        for f in (store.EVENTS, store.STATE):
            f.unlink(missing_ok=True)
        self.job = config.Job(name="backup", every="1h")

    def titles(self):
        return [e["title"] for e in store.events()][::-1]

    def test_failure_then_recovery(self):
        scheduled.ping(self.job, "", "")
        scheduled.ping(self.job, "1", "rsync: connection refused\n")
        self.assertEqual(scheduled.status(self.job)["state"], "failed")
        self.assertEqual(store.events()[0]["log"], ["rsync: connection refused"])
        scheduled.ping(self.job, "0", "")
        self.assertEqual(self.titles(), ["backup failed", "backup worked again"])
        st = scheduled.status(self.job)
        self.assertEqual((st["state"], len(st["runs"])), ("ok", 3))
        self.assertEqual(st["next"], st["last"]["t"] + 3600)
        with self.assertRaises(ValueError):
            scheduled.ping(self.job, "nope", "")

    def test_late_run_alerts_once(self):
        scheduled.record(self.job, True, 0, t=time.time() - 3 * 3600, quiet=True)
        scheduled._check_late(self.job, time.time())
        scheduled._check_late(self.job, time.time())
        self.assertEqual(self.titles(), ["backup didn't run"])
        self.assertEqual(scheduled.status(self.job)["state"], "late")
        scheduled.ping(self.job, "", "")
        scheduled._check_late(self.job, time.time())
        self.assertEqual(self.titles(), ["backup didn't run", "backup ran again"])

    def test_a_new_job_gets_its_interval_before_counting_as_late(self):
        scheduled._check_late(self.job, time.time())
        scheduled._check_late(self.job, time.time() + 1800)
        self.assertEqual(self.titles(), [])
        self.assertEqual(scheduled.status(self.job)["state"], "waiting")
        scheduled._check_late(self.job, time.time() + 3600 + 700)
        self.assertEqual(self.titles(), ["backup didn't run"])

    def test_timer_runs_come_from_systemd(self):
        job = config.Job(name="sync", timer="sync.timer", user=True)
        shows = {
            "sync.timer": "ActiveState=active\nLoadState=loaded\nNextElapseUSecRealtime=Thu 2026-10-08 03:00:00 EDT\nLastTriggerUSec=Wed 2026-10-07 03:00:00 EDT",
            "sync.service": "ActiveState=inactive\nResult=exit-code\nExecMainStatus=2\nExecMainStartTimestamp=Wed 2026-10-07 03:00:00 EDT\nExecMainExitTimestamp=Wed 2026-10-07 03:00:40 EDT",
        }
        fake = lambda cmd, **kw: (0, shows.get(cmd[3], "") if cmd[2] == "show" else "boom")
        with unittest.mock.patch.object(services, "run", side_effect=fake):
            scheduled._poll_timer(job)  # the run from before pi-dash knew the job: shown, not alerted
            self.assertEqual(self.titles(), [])
            st = scheduled.status(job)
            self.assertEqual((st["state"], st["last"]["took"], st["last"]["code"]), ("failed", 40, 2))
            shows["sync.service"] = shows["sync.service"].replace("03:00:40", "04:00:05")
            scheduled._poll_timer(job)
            scheduled._poll_timer(job)  # same run again: nothing new
        self.assertEqual(self.titles(), ["sync failed"])
        self.assertEqual(len(scheduled.status(job)["runs"]), 2)
        self.assertEqual(scheduled.units(), set())  # not registered


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

    def test_page_links_carry_the_version(self):
        code, html, _ = self.request("/", relayed=False)
        v = web.static_version()
        self.assertRegex(v, r"^[0-9a-f]{12}$")
        self.assertIn(f'/static/app.js?v={v}"', html)
        self.assertIn(f'/static/app.css?v={v}"', html)
        self.assertEqual(self.request("/static/app.js?v=" + v)[0], 200)

    def test_version_follows_the_files(self):
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(web, "STATIC", Path(d)):
            (Path(d) / "app.js").write_text("one")
            first = web.static_version()
            self.assertEqual(web.static_version(), first)
            (Path(d) / "app.js").write_text("two!")
            self.assertNotEqual(web.static_version(), first)

    def test_curl_on_the_server_itself_needs_no_password(self):
        self.assertEqual(self.request("/api/jobs/1", relayed=False)[0], 404)

    def test_ping_from_cron_on_the_server(self):
        import urllib.request
        Path(config.REGISTRY).write_text(json.dumps({"services": [], "jobs": [{"name": "backup", "every": "1d"}]}))
        scheduled.reload()
        try:
            req = urllib.request.Request(self.base + "/api/ping/backup/0", data=b"done\n")  # plain curl -d: no header
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(scheduled.last_run(scheduled.get("backup"))["ok"], True)
            self.assertEqual(self.request("/api/ping/nope", "POST", {}, relayed=False)[0], 404)
            self.assertEqual(self.request("/api/ping/backup", "POST", {})[0], 401)  # from elsewhere: signed in only
        finally:
            Path(config.REGISTRY).unlink()
            scheduled.reload()


if __name__ == "__main__":
    unittest.main()
