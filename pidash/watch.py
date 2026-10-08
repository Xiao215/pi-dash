"""Watchers that turn trouble into alerts: crashes, errors in logs, service up/down,
Pi health, restarts of the Pi, and the morning summary. Quiet when all is well."""

import json
import logging
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import config, scheduled, services, store, system
from .notify import ago, alert

log = logging.getLogger(__name__)

_services = {s.name: s for s in config.load_services()}
_last_error_alert: dict[str, float] = {}
_suppressed: dict[str, int] = {}
_last_crash: dict[str, float] = {}
_alerted_down: set[str] = set()
_bad_streak: dict[str, int] = {}
_last_tick = time.time()  # when the sampler last finished a round; the heartbeat only goes out while it keeps going
_last_prune = time.time()  # __main__ prunes at startup
heartbeat = {"ok": None, "t": 0.0, "error": ""}


def reload_services():
    """Read the registry again. If it doesn't load, the error is raised and the old lists stay."""
    global _services
    loaded = {s.name: s for s in config.load_services()}
    scheduled.reload()
    _services = loaded  # a new dict, so other threads can finish going through the old one


def services_list():
    return list(_services.values())


# ---- crashes: Docker events ------------------------------------------------------

def _docker_events():
    if not shutil.which("docker", path=services.ENV["PATH"]):
        log.info("Docker isn't installed; not watching containers")
        return
    while True:
        p = subprocess.Popen(["docker", "events", "--filter", "type=container", "--format", "{{json .}}"],
                             stdout=subprocess.PIPE, text=True, env=services.ENV)
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            try:
                _docker_event(ev)
            except Exception:  # noqa: BLE001 - one bad event mustn't stop the watcher
                log.exception("handling a Docker event failed")
        p.wait()
        time.sleep(5)


def _docker_event(ev):
    """React to one container event: a crash, running out of memory, a failing health check."""
    action = ev.get("Action") or ev.get("status") or ""
    if action not in ("die", "oom") and not action.startswith("health_status: unhealthy"):
        return  # starts, health-check execs and the like: the bulk of the events
    attrs = ev.get("Actor", {}).get("Attributes", {})
    name = attrs.get("name", "?")
    svc = next((s for s in _services.values() if s.kind == "compose" and (s.container or s.name) == name), None)
    label = svc.name if svc else name
    if not svc and not _long_running(name):
        return  # one-off/test containers (no restart policy) aren't services
    if svc and services.running_job(svc):
        return  # replaced on purpose by an update/restart, not a crash
    if action == "die":
        code = attrs.get("exitCode", "0")
        if code in ("0", "143") or label in store.load_state().get("stopped", []):
            return  # normal stop
        _crashed(label, f"Exited with code {code}.", svc)
    elif action == "oom":
        _crashed(label, "Killed: it ran out of memory.", svc)
    elif action.startswith("health_status: unhealthy"):
        if svc or label in _alerted_down:
            return  # the sampler reports configured services, once they've had time to settle
        alert("warn", f"{label} is unhealthy", "Its health check keeps failing.", label)
        _alerted_down.add(label)


def _minutes_before_alert(state, since, now=None):
    """Down: 2 bad samples in a row. Unhealthy just after a start (an update, a restart): 5, since many apps
    are slow to answer while they warm up, and a version that stays broken is still reported."""
    now = now or time.time()
    return 5 if state == "unhealthy" and since and now - since < 600 else 2


def _long_running(container):
    code, out = services.run(["docker", "inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", container], timeout=10)
    return code == 0 and out.strip() not in ("", "no")


def _crashed(label, why, svc):
    now = time.time()
    with store.edit_state() as state:
        crashes = state.setdefault("crashes", {})
        recent = crashes[label] = [t for t in crashes.get(label, []) if now - t < 600] + [now]
    if now - _last_crash.get(label, 0) < 600 and len(recent) < 4:
        store.add_event("error", f"{label} crashed again", why, label)
        return
    _last_crash[label] = now
    lines = services.recent_logs(svc, 20) if svc else []
    if len(recent) >= 4:
        alert("error", f"{label} keeps crashing", f"{len(recent)} crashes in 10 minutes. {why}", label, log_lines=lines)
    else:
        alert("error", f"{label} crashed", f"{why} It restarts automatically.", label, log_lines=lines)
    _alerted_down.add(label)


# ---- errors in logs + systemd unit failures: the journal ------------------------

def _match_service(entry):
    container = entry.get("CONTAINER_NAME")
    user_unit = entry.get("_SYSTEMD_USER_UNIT") or entry.get("USER_UNIT")
    unit = entry.get("_SYSTEMD_UNIT") or entry.get("UNIT")
    for s in _services.values():
        if s.kind == "compose" and container and container == (s.container or s.name):
            return s
        if s.kind == "systemd-user" and user_unit == s.unit:
            return s
        if s.kind == "systemd" and unit == s.unit:
            return s
    return None


_LOG_PREFIX = re.compile(r"^(?:\[?\d{4}-\d\d-\d\d[T ][\d:]{8}(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?\]?[\s:-]*)?"
                         r"(?:\[?(?:ERROR|CRITICAL|FATAL)\]?(?:[\s:-]+|$))?")


def _error_summary(lines, extra=0):
    """The error itself, minus the timestamp and level it's logged with: what tells one alert from the next."""
    first = _LOG_PREFIX.sub("", lines[0]).strip() if lines else ""
    summary = first[:200] + ("…" if len(first) > 200 else "") or "New error lines."
    if extra:
        summary += f"\n+{extra} more since the last alert."
    return summary


def _journal():
    pending: dict[str, dict] = {}  # service -> {"lines": [...], "until": t}
    lock = threading.Lock()

    def flush():
        while True:
            time.sleep(1)
            now = time.time()
            with lock:
                ready = [k for k, v in pending.items() if v["until"] <= now]
                batches = [(k, pending.pop(k)) for k in ready]
            for name, batch in batches:
                extra = _suppressed.pop(name, 0)
                try:
                    alert("warn", f"Errors in {name}'s log", _error_summary(batch["lines"], extra), name,
                          log_lines=batch["lines"][-25:])
                except Exception:  # noqa: BLE001
                    log.exception("alerting about %s's log failed", name)

    threading.Thread(target=flush, name="error-flush", daemon=True).start()

    while True:
        p = subprocess.Popen(["journalctl", "-f", "-n", "0", "-o", "json", "--all"],
                             stdout=subprocess.PIPE, text=True, env=services.ENV)
        for line in p.stdout:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            try:
                _journal_entry(entry, pending, lock)
            except Exception:  # noqa: BLE001 - one bad entry mustn't stop the watcher
                log.exception("handling a journal entry failed")
        p.wait()
        time.sleep(5)


def _journal_entry(entry, pending, lock):
    """One journal line: a unit that failed, or an error in a service's log (with the lines that follow it)."""
    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):
        msg = bytes(msg).decode("utf-8", "replace")
    svc = _match_service(entry)

    # systemd telling us a unit failed (system units too, e.g. a failed update)
    from_manager = entry.get("_PID") == "1" or entry.get("_COMM") == "systemd"
    if from_manager and "Failed with result" in msg:
        if (entry.get("UNIT") or entry.get("USER_UNIT")) in scheduled.units():
            return  # a scheduled job: scheduled.py reports it as "<job> failed"
        name = svc.name if svc else (entry.get("UNIT") or entry.get("USER_UNIT") or "a system service")
        if svc and name in store.load_state().get("stopped", []):
            return
        _crashed(name, msg.strip(), svc)
        return
    if from_manager or not svc:
        return

    name = svc.name
    with lock:
        if name in pending:
            pending[name]["lines"].append(msg.rstrip())
            return
    if services.ERROR_RE.search(msg):
        now = time.time()
        if now - _last_error_alert.get(name, 0) < config.ERROR_COOLDOWN:
            _suppressed[name] = _suppressed.get(name, 0) + 1
            return
        _last_error_alert[name] = now
        with lock:  # gather the traceback that follows for a few seconds
            pending[name] = {"lines": [msg.rstrip()], "until": now + 3}


# ---- up/down: sampled every minute, also feeds the uptime bars -----------------

def _sampler():
    global _last_tick
    net_prev = (system.net_totals(), time.monotonic())
    offline_since = None
    while True:
        try:
            net_prev, offline_since = _sample_round(net_prev, offline_since)
            _last_tick = time.time()
        except Exception:  # noqa: BLE001 - try again next minute; the heartbeat stops if this keeps failing
            log.exception("sampling failed")
        time.sleep(config.SAMPLE_EVERY)


def _sample_round(net_prev, offline_since):
    states, res = {}, {}
    for svc in list(_services.values()):
        try:
            states[svc.name], res[svc.name] = _sample_service(svc)
        except Exception:  # noqa: BLE001
            log.exception("status failed for %s", svc.name)
            states[svc.name], res[svc.name] = "unknown", [None, None, None]
    snap = system.snapshot()
    net, now = system.net_totals(), time.monotonic()
    (rx0, tx0), t0 = net_prev
    rx, tx = ((b - a) / (now - t0) if b >= a and now > t0 else None for a, b in zip((rx0, tx0), net))
    online = system.online()
    store.add_sample(states, {"cpu": snap["cpu"], "temp": round(snap["temp"], 1) if snap["temp"] is not None else None,
                              "mem": round(100 - 100 * snap["mem"]["available"] / snap["mem"]["total"], 1),
                              "rx": round(rx) if rx is not None else None, "tx": round(tx) if tx is not None else None,
                              "online": online}, res)
    return (net, now), _internet(online, offline_since)


def _sample_service(svc):
    """One service's state and [cpu %, memory MB, health check ms] for this minute; alerts when it went down
    or came back."""
    services.check_health(svc)
    services.run_check(svc)
    s = services.status(svc)
    st = s["state"]
    h = services.health.get(svc.name) or {}
    sample = [s.get("cpu"), round(s["mem"] / 2 ** 20, 1) if s.get("mem") is not None else None,
              h["ms"] if h.get("ok") else None]
    bad = st in ("down", "unhealthy") and not services.running_job(svc)  # not mid-update
    _bad_streak[svc.name] = _bad_streak.get(svc.name, 0) + 1 if bad else 0
    if bad and _bad_streak[svc.name] >= _minutes_before_alert(st, s.get("since")) and svc.name not in _alerted_down:
        c = services.checks.get(svc.name)
        if st == "unhealthy" and c and not c["ok"] and h.get("ok", True):
            alert("warn", f"{svc.name} needs attention", c["problem"], svc.name)  # running, but e.g. signed out
        elif st == "unhealthy":
            why = f"It's running, but its health check is failing: {h['error']}." if h.get("error") \
                else "It's running, but its health check keeps failing."
            alert("warn", f"{svc.name} isn't responding", why, svc.name, log_lines=services.recent_logs(svc, 15))
        else:
            alert("error", f"{svc.name} is down", "It isn't running.", svc.name, log_lines=services.recent_logs(svc, 15))
        _alerted_down.add(svc.name)
    elif st == "up" and svc.name in _alerted_down:
        _alerted_down.discard(svc.name)
        alert("ok", f"{svc.name} is back up", service=svc.name)
    return st, sample


def _internet(online, offline_since):
    """Note outages of the Pi's internet. Discord can't be reached meanwhile, so it hears afterwards."""
    now = time.time()
    if not online:
        return offline_since or now
    if offline_since and now - offline_since >= 120:
        alert("warn", "Internet was down", f"For about {ago(now - offline_since)}, from {datetime.fromtimestamp(offline_since):%H:%M} "
              f"to {datetime.now():%H:%M}. Alerts from that time arrive late.")
    return None


# ---- outside heartbeat: someone else notices when this Pi goes quiet ---------------------

def _heartbeat():
    while True:
        if time.time() - _last_tick < 5 * 60:  # watchers still working: say so
            try:
                req = urllib.request.Request(config.HEARTBEAT_URL, headers={"User-Agent": "pi-dash"})
                urllib.request.urlopen(req, timeout=15).close()
                heartbeat.update(ok=True, t=time.time(), error="")
            except Exception as e:  # noqa: BLE001 - offline, DNS, HTTP errors: try again next time
                heartbeat.update(ok=False, t=time.time(), error=str(getattr(e, "reason", e))[:200])
        time.sleep(config.HEARTBEAT_EVERY)


# ---- scheduled jobs --------------------------------------------------------------------

def _jobs_loop():
    time.sleep(10)
    while True:
        try:
            scheduled.poll()
        except Exception:  # noqa: BLE001
            log.exception("checking scheduled jobs failed")
        time.sleep(60)


# ---- auto-update: new commit on GitHub or newly published image -----------------------

def _update_loop():
    time.sleep(20)  # let services settle after a pi-dash or Pi restart
    while True:
        for svc in list(_services.values()):
            try:
                services.refresh_source(svc)
                _maybe_auto_update(svc)
            except Exception:  # noqa: BLE001
                log.exception("update check failed for %s", svc.name)
        time.sleep(config.UPDATE_EVERY)


def _remember(key, name, value):
    with store.edit_state() as state:
        d = state.setdefault(key, {})
        if value is None:
            d.pop(name, None)
        else:
            d[name] = value


def _maybe_auto_update(svc):
    src = services.source.get(svc.name)
    if not src or not src["behind"] or not services.auto_update_on(svc) or services.running_job(svc):
        return
    state = store.load_state()
    target = src.get("target")
    if svc.name in state.get("stopped", []) or state.get("failed_updates", {}).get(svc.name) == target:
        return  # stopped on purpose, or this exact version already failed: wait for a newer one
    if src["dirty"]:
        if state.get("dirty_warned", {}).get(svc.name) != target:
            _remember("dirty_warned", svc.name, target)
            alert("warn", f"Can't auto-update {svc.name}", "Its folder on the Pi has local code changes, so pulling "
                  "would clash. Commit or discard them, then press Update.", svc.name)
        return

    before = src["sha"]
    job = services.start_job(svc, "update", auto=True)
    while not job["done"]:
        time.sleep(2)
    state_now = "unknown"
    if job["ok"]:
        deadline = time.time() + 180  # give it up to 3 minutes to come back healthy
        while time.time() < deadline:
            services.check_health(svc)
            state_now = services.status(svc)["state"]
            if state_now == "up":
                break
            time.sleep(5)
    after = services.source.get(svc.name, {})
    if job["ok"] and state_now == "up":
        _remember("failed_updates", svc.name, None)
        if after.get("kind") == "git":
            detail = f"{before} → {after.get('sha')}: {after.get('subject', '')}"
        else:
            detail = f"Now running the image published {ago(time.time() - after['time'])} ago." if after.get("time") else "Now running the newest image."
        freed = services.prune_images() if svc.kind == "compose" else ""  # only now that the new version works
        if freed:
            detail += f"\nFreed {freed} of old images."
        alert("update", f"{svc.name} updated", detail, svc.name)
        return
    _remember("failed_updates", svc.name, target)
    if not job["ok"]:
        why, lines = "The update steps failed (output below). It keeps running the old version.", job["lines"][-20:]
    else:
        why, lines = f"The new version didn't come back healthy within 3 minutes (now: {state_now}).", services.recent_logs(svc, 20)
        _alerted_down.add(svc.name)  # so you hear when it recovers
    alert("error", f"Auto-update of {svc.name} failed", why + " This version won't be retried; push a fix "
          "or press Update to try again.", svc.name, log_lines=lines)


# ---- the Pi's health, every 10 minutes; alerts only on change --------------------

def _check(key, bad: bool, level, title, detail, ok_title, service=None):
    with store.edit_state() as state:
        problems = state.setdefault("problems", {})
        began = bad and key not in problems
        since = None if bad else problems.pop(key, None)
        if began:
            problems[key] = time.time()
    if began:
        alert(level, title, detail, service)
    elif since is not None:
        alert("ok", ok_title, f"Lasted {ago(time.time() - since)}.", service)


def memory_growth(rows, name, chunks=6):
    """(first, last) hourly median memory in MB when a service's memory rose steadily through the rows
    (by half and 150 MB at least, never dropping back), else None. A restart drops it, so that doesn't count."""
    pts = [(r["t"], r["r"][name][1]) for r in rows if (r.get("r", {}).get(name) or [None, None])[1] is not None]
    if len(pts) < 50 * chunks or pts[-1][0] - pts[0][0] < 0.9 * chunks * 3600:
        return None
    t0, span = pts[0][0], (pts[-1][0] - pts[0][0]) / chunks
    parts = [[] for _ in range(chunks)]
    for t, mb in pts:
        parts[min(chunks - 1, int((t - t0) / span))].append(mb)
    if not all(parts):
        return None
    med = [statistics.median(p) for p in parts]
    steady = all(b >= a * 0.97 for a, b in zip(med, med[1:]))
    if steady and med[-1] >= 1.5 * med[0] and med[-1] - med[0] >= 150:
        return med[0], med[-1]
    return None


def _check_memory_growth():
    rows = store.samples(time.time() - 6 * 3600)
    for svc in list(_services.values()):
        grew = memory_growth(rows, svc.name)
        detail = (f"From {grew[0]:.0f} MB to {grew[1]:.0f} MB over 6 hours without a restart. It may be leaking memory; "
                  "restarting it frees it for now.") if grew else ""
        _check(f"leak:{svc.name}", bool(grew), "warn", f"{svc.name}'s memory keeps growing", detail,
               f"{svc.name}'s memory stopped growing", svc.name)


def _health_once():
    snap = system.snapshot()
    disk_pct = 100 * snap["disk"]["used"] / snap["disk"]["total"]
    _check("disk", disk_pct >= config.DISK_WARN, "warn" if disk_pct < config.DISK_WARN + 10 else "error", f"Disk {disk_pct:.0f}% full",
           f"{snap['disk']['free'] / 1e9:.1f} GB left on the SD card.", "Disk space is fine again")
    t = snap["temp"]
    hot = t is not None and t >= config.TEMP_WARN
    _check("temp", hot, "error" if hot and t >= config.TEMP_WARN + 5 else "warn", f"Pi is hot: {t or 0:.0f} °C",
           "It slows itself down above 80 °C. Check airflow or add a fan.", "Temperature is back to normal")
    mem = snap["mem"]
    mem_pct = 100 * mem["available"] / mem["total"]
    _check("memory", mem_pct < config.MEM_FREE_WARN, "warn", f"Memory almost full: {mem_pct:.0f}% free",
           "Services may get killed if it runs out.", "Memory is fine again")
    flags = snap["power"]["flags"]
    now_flags = [f for f in flags if f.endswith("now")]
    _check("power", bool(now_flags), "error", "Power problem: " + ", ".join(now_flags),
           "The power supply can't keep up. Use the official 5V/3A supply and a short cable.", "Power is stable again")
    state = store.load_state()
    if "under-voltage since boot" in flags and state.get("undervolt_boot") != system.boot_id():
        store.update_state(undervolt_boot=system.boot_id())
        if not now_flags:
            alert("warn", "Power dipped since the last restart",
                  "The Pi saw low voltage at some point. A weak supply can corrupt the SD card over time.")
    failed = set()
    for cmd in (["systemctl", "--failed"], ["systemctl", "--user", "--failed"]):
        code, out = services.run([*cmd, "--plain", "--no-legend"])
        if code == 0:  # e.g. no user manager: an error message, not a list of units
            failed.update(l.split()[0] for l in out.splitlines() if l.strip())
    failed = sorted(failed - scheduled.units())
    _check("failed-units", bool(failed), "warn", "Failed: " + ", ".join(failed)[:200],
           "systemd marks these as failed. See them on the dashboard or with systemctl --failed.",
           "No failed services anymore")
    _check_updates()
    _check_memory_growth()
    system.refresh_updates()
    _daily_docker_cleanup()
    _daily_prune()


def _daily_prune():
    global _last_prune
    if time.time() - _last_prune >= 86400:
        _last_prune = time.time()
        store.prune()


def _daily_docker_cleanup():
    """Updates clean up after themselves; this also catches builds and pulls done by hand."""
    if not shutil.which("docker", path=services.ENV["PATH"]):
        return
    if time.time() - store.load_state().get("docker_cleaned", 0) < 86400:
        return
    store.update_state(docker_cleaned=time.time())
    freed = services.prune_images()
    if freed:
        alert("info", "Cleaned up Docker", f"Freed {freed} of old images and build cache.", discord=False)


def _check_updates():
    path = Path("/var/log/unattended-upgrades/unattended-upgrades.log")
    try:
        size = path.stat().st_size
    except OSError:
        return
    state = store.load_state()
    offset = state.get("uu_offset", size)
    if size < offset:
        offset = 0  # rotated
    with path.open(errors="replace") as f:
        f.seek(offset)
        new = f.read()
    store.update_state(uu_offset=size)
    errors = [l for l in new.splitlines() if " ERROR " in l or "Traceback" in l]
    if errors:
        alert("error", "Automatic update failed", "unattended-upgrades reported errors.", log_lines=errors[-15:])


def _health():
    while True:
        try:
            _health_once()
        except Exception:  # noqa: BLE001
            log.exception("health check failed")
        time.sleep(config.HEALTH_EVERY)


# ---- restarts of the Pi -----------------------------------------------------------

def _boot_reason():
    """Why did the previous boot end? Reads the end of the last boot's journal."""
    _, out = services.run(["journalctl", "-b", "-1", "-n", "400", "-o", "json", "--no-pager"], timeout=30)
    msgs, last_t = [], None
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("MESSAGE", "")
        msgs.append(m if isinstance(m, str) else "")
        last_t = int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6
    text = "\n".join(msgs)
    planned = store.load_state().get("planned")
    if planned and last_t and abs(planned["t"] - last_t) < 900:
        return ("restarted from the dashboard" if planned["action"] == "reboot"
                else "shut down from the dashboard, then turned back on"), last_t
    if not msgs:
        return "unknown (no log from before the restart)", None
    uu_log = Path("/var/log/unattended-upgrades/unattended-upgrades.log")
    try:
        uu_tail = uu_log.read_text()[-3000:]
    except OSError:
        uu_tail = ""
    planned_reboot = "System is rebooting" in text or "reboot.target" in text
    if planned_reboot and last_t and "rebooting" in uu_tail.lower() and f"{datetime.fromtimestamp(last_t):%Y-%m-%d %H}" in uu_tail:
        return "scheduled restart after an automatic update", last_t
    if planned_reboot:
        return "restarted on purpose (someone ran reboot)", last_t
    if "System is powering down" in text or "poweroff.target" in text:
        return "it was shut down on purpose, then turned back on", last_t
    return "unexpected: power was cut or it crashed", last_t


def _clock_synced():
    code, out = services.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return code != 0 or out.strip() == "yes"  # no timedatectl: nothing to wait for, trust the clock


def _downtime(last_t):
    """Seconds between the last log line of the previous boot and this boot, or None if unknown.
    A Pi has no battery clock: it boots with the last time it saved, which runs behind until NTP
    syncs, so the boot looks earlier than it was (even before last_t). Wait for the sync; with no
    network the alert can't go out anyway."""
    for _ in range(30):
        if _clock_synced():
            down = time.time() - system.uptime() - last_t
            return down if down >= 0 else None
        time.sleep(10)
    return None


def _announce_boot():
    state = store.load_state()
    current = system.boot_id()
    if state.get("boot_id") == current:
        return
    first = "boot_id" not in state
    time.sleep(90)  # give services a moment so the message can say whether they came back
    rows = []
    for svc in _services.values():
        st = services.status(svc)["state"]
        rows.append(f"• {svc.name}: {'running' if st == 'up' else st}")
    summary = "\n".join(rows) or "No services registered."
    if first:
        morning = f", plus a summary every morning at {config.DIGEST_HOUR}:00" if config.DIGEST_HOUR >= 0 else ""
        alert("boot", "pi-dash is watching this Pi", f"You'll hear from me only when something needs you{morning}.\n\n{summary}")
    else:
        reason, last_t = _boot_reason()
        down = _downtime(last_t) if last_t else None
        off = f"\nWas down for about {ago(down)}." if down is not None else ""
        level = "error" if reason.startswith("unexpected") else "boot"
        alert(level, "Pi restarted", f"Reason: {reason}.{off}\n\n{summary}")
    store.update_state(boot_id=current, planned=None)  # only once announced, so a pi-dash restart mid-wait still announces


# ---- morning summary --------------------------------------------------------------

def uptime_pct(name, since, rows=None):
    rows = [r["s"].get(name) for r in (store.samples(since) if rows is None else rows)]
    rows = [r for r in rows if r and r != "stopped"]
    return 100 * sum(r == "up" for r in rows) / len(rows) if rows else None


def _updates_since(since):
    count = 0
    try:
        text = Path("/var/log/apt/history.log").read_text()
    except OSError:
        return 0
    for block in text.split("\n\n"):
        m = re.search(r"Start-Date: (\S+)\s+(\S+)", block)
        if not m:
            continue
        try:
            t = datetime.strptime(f"{m[1]} {m[2]}", "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if t >= since:
            for kind in ("Upgrade:", "Install:"):
                for line in block.splitlines():
                    if line.startswith(kind):
                        count += line.count("),") + 1
    return count


def send_digest():
    since = time.time() - 86400
    snap = system.snapshot()
    rows = []
    for svc in _services.values():
        s = services.status(svc)
        pct = uptime_pct(svc.name, since)
        pct_text = f"{pct:.1f}% up" if pct is not None else "no data yet"
        state = "running" if s["state"] == "up" else s["state"]
        rows.append(f"• **{svc.name}**: {state} · {pct_text} · {s.get('restarts', 0)} restarts")
    incidents = [e for e in store.events(500, since) if e["level"] in ("error", "warn")]
    disk_pct = 100 * snap["disk"]["used"] / snap["disk"]["total"]
    fields = [
        ("Pi", f"up {ago(snap['uptime'])} · " + (f"{snap['temp']:.0f} °C · " if snap["temp"] is not None else "") + f"disk {disk_pct:.0f}% · "
               f"memory {100 - 100 * snap['mem']['available'] / snap['mem']['total']:.0f}% used", False),
        ("Last 24 h", f"{len(incidents)} problem{'s' if len(incidents) != 1 else ''} · "
                      f"{_updates_since(since)} packages updated" + _pending_text(snap) +
                      (" · restart pending" if snap["reboot_required"] else ""), False),
    ]
    jobs = [scheduled.status(j) for j in scheduled.jobs_list()]
    if jobs:
        fields.append(("Scheduled jobs", "\n".join(f"• **{j['name']}**: " + _job_text(j) for j in jobs), False))
    if incidents:
        fields.append(("Problems", "\n".join(f"• {e['title']}" for e in incidents[:8]), False))
    alert("digest", "Good morning, here's your Pi", "\n".join(rows) or "No services registered.", fields=fields)


def _pending_text(snap):
    u = snap.get("updates")
    if not u or not u["count"]:
        return ""
    return f" · {u['count']} update{'s' if u['count'] != 1 else ''} waiting" + (f" ({u['security']} security)" if u["security"] else "")


def _job_text(j):
    last = j["last"]
    if j["state"] in ("failed", "late", "off", "missing"):
        word = {"failed": "last run failed", "late": "didn't run on time", "off": "timer is off", "missing": "timer not found"}
        return word[j["state"]]
    if not last:
        return "hasn't run yet"
    return f"ran {ago(time.time() - last['t'])} ago"


def _digest():
    if config.DIGEST_HOUR < 0:
        return
    while True:
        now = datetime.now()
        nxt = now.replace(hour=config.DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            send_digest()
        except Exception:  # noqa: BLE001
            log.exception("digest failed")


def start():
    targets = [_docker_events, _journal, _sampler, _health, _announce_boot, _digest, _update_loop, _jobs_loop]
    if config.HEARTBEAT_URL:
        targets.append(_heartbeat)
    for target in targets:
        threading.Thread(target=target, name=target.__name__.strip("_"), daemon=True).start()
