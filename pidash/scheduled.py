"""Scheduled jobs: systemd timers, and anything that pings /api/ping/<name> when it finishes (cron jobs,
scripts on other machines). Alerts when a run fails, and when one doesn't happen on time."""

import re
import subprocess
import time
from types import SimpleNamespace

from . import config, services, store
from .notify import alert

NAME_RE = re.compile(r"^[\w.-]{1,64}$")

_jobs = {j.name: j for j in config.load_jobs()}
timers: dict[str, dict] = {}  # name -> what systemd says about the timer and its last run


def reload():
    _jobs.clear()
    _jobs.update({j.name: j for j in config.load_jobs()})


def jobs_list():
    return list(_jobs.values())


def get(name):
    return _jobs.get(name)


def units() -> set[str]:
    """The services timers start; their failures are reported here as "<job> failed", not as crashes."""
    return {j.service for j in _jobs.values() if j.service}


def _systemctl(job, sudo=False):
    if job.user:
        return ["systemctl", "--user"]
    return ["sudo", "-n", "systemctl"] if sudo else ["systemctl"]


def as_service(job):
    """Enough of a Service for services.log_command, to read a timer's logs."""
    return SimpleNamespace(name=job.name, kind="systemd-user" if job.user else "systemd", unit=job.service, container="")


# ---- runs ---------------------------------------------------------------------------

def record(job, ok: bool, code=None, took=None, log_lines=None, t=None, quiet=False):
    """Remember one finished run and alert when it failed, or when it worked again after a problem."""
    t = t or time.time()
    state = store.load_state()
    runs = state.get("job_runs", {})
    runs[job.name] = (runs.get(job.name, []) + [{"t": t, "ok": ok, "code": code, "took": took}])[-30:]
    logs = state.get("job_logs", {})
    if log_lines is not None:
        logs[job.name] = [l[:400] for l in log_lines[-40:]]
    problems = state.get("job_problems", {})
    before = problems.get(job.name)
    if ok:
        if before in ("failed", "late"):  # "off" stays until the timer is on again
            problems.pop(job.name)
    else:
        problems[job.name] = "failed"
    store.update_state(job_runs=runs, job_logs=logs, job_problems=problems)
    if quiet:
        return
    if not ok:
        why = f"Exited with code {code}." if code not in (None, 0) else "It reported a failure."
        alert("error", f"{job.name} failed", why, job.name, log_lines=log_lines)
    elif before == "failed":
        alert("ok", f"{job.name} worked again", service=job.name)
    elif before == "late":
        alert("ok", f"{job.name} ran again", service=job.name)


def ping(job, status: str, body: str):
    """POST /api/ping/<name>[/<status>]: status is empty or 0 for success, "fail" or an exit code otherwise."""
    if status in ("", "ok", "success"):
        code = 0
    elif status == "fail":
        code = None
    elif status.isdigit():
        code = int(status)
    else:
        raise ValueError("status must be an exit code or 'fail'")
    lines = body.splitlines() if body.strip() else None
    record(job, code == 0, code, log_lines=lines)


# ---- systemd timers, polled every minute ---------------------------------------------

def _show(cmd, unit, props):
    _, out = services.run([*cmd, "show", unit, "-p", props], timeout=10)
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _poll_timer(job):
    cmd = _systemctl(job)
    t = _show(cmd, job.timer, "ActiveState,LoadState,NextElapseUSecRealtime,LastTriggerUSec")
    s = _show(cmd, job.service, "ActiveState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp")
    parse = services._parse_systemd_time
    info = {
        "found": t.get("LoadState") == "loaded",
        "active": t.get("ActiveState") == "active",
        "next": parse(t.get("NextElapseUSecRealtime")),
        "last": parse(t.get("LastTriggerUSec")),
        "running": s.get("ActiveState") in ("activating", "active", "deactivating"),
        "started": parse(s.get("ExecMainStartTimestamp")),
        "finished": parse(s.get("ExecMainExitTimestamp")),
        "result": s.get("Result"),
        "code": int(s.get("ExecMainStatus") or 0),
    }
    timers[job.name] = info
    finished = info["finished"]
    if not finished or info["running"]:
        return
    seen = store.load_state().get("job_seen_runs", {})
    if seen.get(job.name) == finished:
        return
    first = job.name not in seen
    seen[job.name] = finished
    store.update_state(job_seen_runs=seen)
    ok = info["result"] == "success"
    took = finished - info["started"] if info["started"] and finished >= info["started"] else None
    lines = None if ok else services.recent_logs(as_service(job), 30)  # the Logs button streams the journal anyway
    record(job, ok, info["code"], took, lines, t=finished, quiet=first)  # the run before pi-dash knew: just show it


def last_run(job):
    runs = store.load_state().get("job_runs", {}).get(job.name, [])
    return runs[-1] if runs else None


def _check_late(job, now):
    """Alert once when a run is overdue, or the timer was switched off."""
    state = store.load_state()
    problems = state.get("job_problems", {})
    seen = state.get("job_seen", {})
    if job.name not in seen:
        seen[job.name] = now
        store.update_state(job_seen=seen)
    timer = timers.get(job.name) if job.timer else None
    late = off = False
    if timer is not None and timer["found"] and not timer["active"]:
        off = True
    elif job.every:
        last = last_run(job)
        since = max(last["t"] if last else 0, (timer or {}).get("last") or 0) or seen[job.name]
        late = now > since + job.every + job.grace and not (timer or {}).get("running")
    current = problems.get(job.name)
    if late or off:
        if current in ("late", "off", "failed"):
            return
        problems[job.name] = "off" if off else "late"
        store.update_state(job_problems=problems)
        if off:
            alert("warn", f"{job.name}'s timer is off", f"{job.timer} isn't active, so it won't run. "
                  f"Turn it back on with systemctl{' --user' if job.user else ''} enable --now {job.timer}.", job.name)
        else:
            alert("warn", f"{job.name} didn't run", f"It should run every {_human(job.every)}; "
                  f"the last run was {_ago_text(since, now)}.", job.name)
    elif current in ("late", "off"):
        problems.pop(job.name)
        store.update_state(job_problems=problems)
        alert("ok", f"{job.name}'s timer is on again" if current == "off" else f"{job.name} ran again", service=job.name)


def _human(sec):
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if sec >= n and sec % n == 0:
            k = sec // n
            return f"{k} {unit}s" if k > 1 else unit
    return f"{sec} s"


def _ago_text(t, now):
    from .watch import _ago
    return f"{_ago(now - t)} ago"


def poll():
    now = time.time()
    for job in list(_jobs.values()):
        if job.timer:
            _poll_timer(job)
        _check_late(job, now)


# ---- what the page shows ---------------------------------------------------------------

def status(job, now=None) -> dict:
    now = now or time.time()
    state = store.load_state()
    runs = state.get("job_runs", {}).get(job.name, [])
    timer = timers.get(job.name) if job.timer else None
    last = runs[-1] if runs else None
    problem = state.get("job_problems", {}).get(job.name)
    if timer and timer["running"]:
        st = "running"
    elif timer and not timer["found"]:
        st = "missing"
    elif problem:
        st = problem  # failed | late | off
    elif last:
        st = "ok"
    else:
        st = "waiting"
    nxt = (timer or {}).get("next")
    if not nxt and job.every and last:
        nxt = last["t"] + job.every
    return {"name": job.name, "description": job.description, "kind": "timer" if job.timer else "ping",
            "timer": job.timer, "user": job.user, "every": job.every, "grace": job.grace, "state": st,
            "last": last, "next": nxt, "runs": runs[-14:], "can_run": bool(job.timer),
            "has_log": bool(job.timer or state.get("job_logs", {}).get(job.name))}


def run_now(job):
    subprocess.Popen([*_systemctl(job, sudo=True), "start", "--no-block", job.service], env=services.ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
