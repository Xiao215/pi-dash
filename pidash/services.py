"""Each service's status, resource use and actions, for Docker Compose and systemd services."""

import calendar
import itertools
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config, store

HOME = Path.home()
ENV = {
    **os.environ,
    "PATH": ":".join([*config.EXTRA_PATH, "/usr/local/bin", "/usr/bin", "/bin"]),
    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
    "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
}
# Lines that mean something went wrong: Python tracebacks, ERROR/CRITICAL/FATAL log levels,
# JavaScript "TypeError: ..." style errors, unhandled promise rejections, Go panics.
ERROR_RE = re.compile(r"Traceback \(most recent call last\)|\b(ERROR|CRITICAL|FATAL)\b|\b[A-Z]\w*Error: |"
                      r"Unhandled(Promise)?Rejection|uncaughtException|^panic: ")

health: dict[str, dict] = {}       # name -> {"ok": bool, "ms": int, "error": str, "t": float}
source: dict[str, dict] = {}       # name -> what version runs and whether a newer one exists
checks: dict[str, dict] = {}       # name -> {"ok": bool, "problem": str, "t": float}
_cpu_prev: dict[str, tuple] = {}   # name -> (usage_usec, monotonic)


def run(cmd, cwd=None, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=ENV)
        return p.returncode, p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _systemctl(svc):
    return ["systemctl", "--user"] if svc.kind == "systemd-user" else ["sudo", "-n", "systemctl"]


def _parse_systemd_time(text):
    if not text or text == "n/a":
        return None
    try:
        return time.mktime(time.strptime(" ".join(text.split()[1:3]), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _cgroup_usage(name, cgroup):
    base = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    try:
        try:
            mem = int((base / "memory.current").read_text())
        except FileNotFoundError:  # memory controller off: add up the processes' resident memory
            mem = 0
            for pid in (base / "cgroup.procs").read_text().split():
                try:
                    status = Path(f"/proc/{pid}/status").read_text()
                    mem += int(status.split("VmRSS:")[1].split()[0]) * 1024
                except (OSError, IndexError, ValueError):
                    pass
        usec = int(next(l for l in (base / "cpu.stat").read_text().splitlines() if l.startswith("usage_usec")).split()[1])
    except (OSError, StopIteration, ValueError):
        return None, None
    now = time.monotonic()
    prev = _cpu_prev.get(name)
    _cpu_prev[name] = (usec, now)
    cpu = None
    if prev and now > prev[1]:
        cpu = round(100 * (usec - prev[0]) / ((now - prev[1]) * 1e6) / (os.cpu_count() or 1), 1)  # % of the whole Pi
    return mem, cpu


def _compose_status(svc):
    code, out = run(["docker", "inspect", svc.container or svc.name], timeout=10)
    if code != 0:
        return {"state": "missing", "detail": "container not created"}
    info = json.loads(out)[0]
    st = info["State"]
    raw = st.get("Status")  # created running paused restarting removing exited dead
    h = (st.get("Health") or {}).get("Status")
    mem, cpu = _cgroup_usage(svc.name, f"system.slice/docker-{info['Id']}.scope") if raw == "running" else (None, None)
    return {
        "raw": raw, "health_status": h,
        "since": _parse_docker_time(st.get("StartedAt") if raw == "running" else st.get("FinishedAt")),
        "exit_code": st.get("ExitCode"), "oom": st.get("OOMKilled"),
        "restarts": info.get("RestartCount", 0), "pid": st.get("Pid"),
        "mem": mem, "cpu": cpu, "image": info.get("Config", {}).get("Image"),
        "log_driver": (info.get("HostConfig", {}).get("LogConfig") or {}).get("Type"),  # pi-dash reads journald only
    }


def _parse_docker_time(ts):
    """Docker's '2026-10-07T04:35:18.912345678Z' (UTC) -> epoch seconds."""
    if not ts or ts.startswith("0001"):
        return None
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _systemd_status(svc):
    props = "ActiveState,SubState,Result,ActiveEnterTimestamp,InactiveEnterTimestamp,NRestarts,ControlGroup,MainPID,ExecMainStatus"
    code, out = run([*_systemctl(svc), "show", svc.unit, "-p", props], timeout=10)
    p = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    active = p.get("ActiveState", "unknown")
    mem, cpu = _cgroup_usage(svc.name, p.get("ControlGroup", "")) if active == "active" and p.get("ControlGroup") else (None, None)
    return {
        "raw": active, "sub": p.get("SubState"), "result": p.get("Result"),
        "since": _parse_systemd_time(p.get("ActiveEnterTimestamp") if active == "active" else p.get("InactiveEnterTimestamp")),
        "restarts": int(p.get("NRestarts") or 0), "pid": int(p.get("MainPID") or 0),
        "exit_code": int(p.get("ExecMainStatus") or 0), "mem": mem, "cpu": cpu,
    }


def status(svc) -> dict:
    s = _compose_status(svc) if svc.kind == "compose" else _systemd_status(svc)
    raw = s.get("raw")
    h = health.get(svc.name)
    stopped_on_purpose = svc.name in store.load_state().get("stopped", [])
    c = checks.get(svc.name)
    problem = c["problem"] if c and not c["ok"] else ""
    if raw in ("running", "active"):
        if s.get("health_status") == "unhealthy" or (h and not h["ok"]) or problem:
            state = "unhealthy"
        elif s.get("health_status") == "starting":
            state = "starting"
        else:
            state = "up"
    elif raw in ("restarting", "activating", "reloading", "created"):
        state = "starting"
    elif stopped_on_purpose:
        state = "stopped"
    else:
        state = "down"
    return {"name": svc.name, "kind": svc.kind, "description": svc.description, "url": svc.url, "dir": svc.dir,
            "state": state, "health": h, "can_update": bool(svc.update), "source": source.get(svc.name),
            "busy": running_job(svc), "auto_update": auto_update_on(svc),
            "problem": problem if state == "unhealthy" else "", **s}


def check_health(svc):
    if not svc.health:
        health.pop(svc.name, None)
        return
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(svc.health, timeout=5) as r:
            ok, err = 200 <= r.status < 400, ""
    except Exception as e:  # noqa: BLE001 - any failure means unhealthy
        ok, err = False, str(getattr(e, "reason", e))[:200]
    health[svc.name] = {"ok": ok, "ms": int((time.monotonic() - t0) * 1000), "error": err, "t": time.time()}


def run_check(svc, force=False):
    """The service's own extra check (e.g. "is Claude signed in?"), run every `every` seconds."""
    if not svc.check.get("command"):
        checks.pop(svc.name, None)
        return
    prev = checks.get(svc.name)
    if prev and not force and time.time() - prev["t"] < svc.check.get("every", 300):
        return
    code, _ = run(["/bin/bash", "-c", svc.check["command"]], cwd=svc.path if svc.dir else None, timeout=60)
    checks[svc.name] = {"ok": code == 0, "problem": svc.check.get("problem", "Its check is failing."), "t": time.time()}


def refresh_source(svc, fetch=True):
    """Git checkout: current commit vs GitHub. Registry image: running digest vs the published one."""
    if svc.dir and (svc.repo_path / ".git").exists():
        _refresh_git(svc, fetch)
    elif svc.kind == "compose" and svc.update:
        _refresh_image(svc, fetch)
    else:
        source.pop(svc.name, None)


def _refresh_git(svc, fetch):
    repo = str(svc.repo_path)
    fetched = run(["git", "-C", repo, "fetch", "-q"], timeout=60)[0] == 0 if fetch else None
    _, out = run(["git", "-C", repo, "log", "-1", "--format=%h%x00%s%x00%ct"])
    try:
        sha, subject, ct = out.strip().split("\x00")
    except ValueError:
        return
    code, behind = run(["git", "-C", repo, "rev-list", "--count", "HEAD..@{u}"])
    behind = int(behind) if code == 0 and behind.strip().isdigit() else 0
    newest = run(["git", "-C", repo, "log", "-1", "--format=%h %s", "@{u}"])[1].strip() if behind else ""
    target = run(["git", "-C", repo, "rev-parse", "--short", "@{u}"])[1].strip()
    dirty = bool(run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=no"])[1].strip())
    prev = source.get(svc.name, {})
    source[svc.name] = {"kind": "git", "sha": sha, "subject": subject, "time": int(ct), "behind": behind,
                        "newest": newest, "target": target, "dirty": dirty,
                        "checked": time.time() if fetch else prev.get("checked"), "check_failed": fetched is False}


def _git_log(repo, rev, n):
    _, out = run(["git", "-C", repo, "log", f"-{n}", "--format=%h%x00%s%x00%ct%x00%an", rev])
    commits = []
    for line in out.splitlines():
        parts = line.split("\x00")
        if len(parts) == 4 and parts[2].isdigit():
            commits.append({"sha": parts[0], "subject": parts[1], "time": int(parts[2]), "author": parts[3]})
    return commits


def code_history(svc):
    """For a git checkout: the commits waiting upstream that an update would bring, and the latest ones running now."""
    if (source.get(svc.name) or {}).get("kind") != "git":
        return None
    repo = str(svc.repo_path)
    pending = _git_log(repo, "HEAD..@{u}", 20) if source[svc.name].get("behind") else []
    return {"pending": pending, "recent": _git_log(repo, "HEAD", 6)}


def _refresh_image(svc, fetch):
    code, image = run(["docker", "inspect", "-f", "{{.Config.Image}}", svc.container or svc.name], timeout=10)
    image = image.strip()
    if code != 0 or "/" not in image:  # built locally, nothing to compare against
        source.pop(svc.name, None)
        return
    _, out = run(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}|{{.Created}}"], timeout=10)
    try:
        digests, created = out.strip().rsplit("|", 1)
        local = {d.split("@", 1)[1] for d in json.loads(digests)}
    except (ValueError, IndexError):
        local, created = set(), ""
    prev = source.get(svc.name, {})
    remote, check_failed = prev.get("target"), None
    if fetch:
        code, out = run(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest.Digest}}"], timeout=60)
        check_failed = code != 0
        if code == 0:
            remote = json.loads(out.strip() or '""') or remote
    current = next(iter(local), "")
    source[svc.name] = {"kind": "image", "image": image, "sha": current.split(":")[-1][:7],
                        "time": _parse_docker_time(created), "behind": int(bool(remote and remote not in local)),
                        "newest": "a newer image was published" if remote and remote not in local else "",
                        "target": remote, "dirty": False,
                        "checked": time.time() if fetch else prev.get("checked"), "check_failed": check_failed}


def auto_update_on(svc) -> bool:
    return bool(svc.update) and svc.name not in store.load_state().get("auto_update_off", [])


def log_command(svc, lines=200, follow=False):
    if svc.kind == "compose":
        cmd = ["journalctl", f"CONTAINER_NAME={svc.container or svc.name}"]
    elif svc.kind == "systemd-user":  # `--user -u` misses lines when run from a system service
        cmd = ["journalctl", f"_SYSTEMD_USER_UNIT={svc.unit}", "+", f"USER_UNIT={svc.unit}"]
    else:
        cmd = ["journalctl", "-u", svc.unit]
    cmd += ["-o", "json", "-n", str(lines), "--no-pager"]
    return cmd + (["-f"] if follow else [])


def format_entry(entry) -> dict:
    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):  # journald stores non-UTF-8 as a byte list
        msg = bytes(msg).decode("utf-8", "replace")
    t = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6
    return {"t": t, "msg": msg.rstrip(), "error": bool(ERROR_RE.search(msg)), "p": int(entry.get("PRIORITY", 6))}


def recent_logs(svc, lines=20) -> list[str]:
    _, out = run(log_command(svc, lines), timeout=10)
    rows = []
    for line in out.splitlines():
        try:
            rows.append(format_entry(json.loads(line))["msg"])
        except ValueError:
            continue
    return rows


SIZE_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
PRUNE_STEPS = [  # (command, the line that says how much it freed)
    (["docker", "image", "prune", "-f"], r"Total reclaimed space:\s*([\d.]+)\s*([kKMGT]?B)"),
    # build cache that `up --build` piles up; a week's worth stays, so rebuilds stay quick
    (["docker", "builder", "prune", "-f", "--filter", "until=168h"], r"Total:\s*([\d.]+)\s*([kKMGT]?B)"),
]


def prune_images(job=None) -> str:
    """Remove images that no tag points to and no container uses, and build cache older than a week:
    what `compose pull` and `up --build` leave behind. Returns the space freed ("1.2 GB"), or ""."""
    if not config.PRUNE_IMAGES or any(not j["done"] and j["action"] == "update" and j is not job for j in jobs.values()):
        return ""  # another update may be building right now; the next update cleans up
    freed = 0.0
    for cmd, total_re in PRUNE_STEPS:
        code, out = run(cmd, timeout=300)
        m = re.search(total_re, out)
        if job is not None:
            job["lines"] += ["$ " + " ".join(cmd), m[0] if m else (out.strip().splitlines() or [""])[-1]]
        if code == 0 and m:
            freed += float(m[1]) * SIZE_UNITS[m[2]]
    if freed < 1e6:
        return ""
    return f"{freed / 1e9:.1f} GB" if freed >= 1e9 else f"{freed / 1e6:.0f} MB"


# ---- actions -----------------------------------------------------------------

jobs: dict[str, dict] = {}
_ids = itertools.count(1)


def _steps(svc, action):
    if action == "update":
        return svc.update
    if svc.kind == "compose":
        return {"start": ["docker compose up -d"], "stop": ["docker compose stop"],
                "restart": ["docker compose restart"]}[action]
    sysctl = " ".join(_systemctl(svc))
    return [f"{sysctl} {action} {svc.unit}"]


def running_job(svc):
    return next((j["action"] for j in jobs.values() if j["service"] == svc.name and not j["done"]), None)


def start_job(svc, action, auto=False) -> dict:
    job = {"id": str(next(_ids)), "service": svc.name, "action": action, "lines": [],
           "done": False, "ok": None, "started": time.time(), "auto": auto}
    jobs[job["id"]] = job
    for old in sorted(jobs, key=int)[:-30]:
        jobs.pop(old, None)

    stopped = set(store.load_state().get("stopped", []))
    if action == "stop":
        stopped.add(svc.name)
    elif action in ("start", "restart", "update"):
        stopped.discard(svc.name)
    store.update_state(stopped=sorted(stopped))

    def work():
        ok = True
        for step in _steps(svc, action):
            job["lines"].append(f"$ {step}")
            try:
                p = subprocess.Popen(["/bin/bash", "-c", step], cwd=svc.path if svc.dir else None, env=ENV,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    job["lines"].append(line.rstrip())
                ok = p.wait() == 0
            except OSError as e:
                job["lines"].append(str(e))
                ok = False
            if not ok:
                break
        freed = prune_images(job) if ok and action == "update" and svc.kind == "compose" and not auto else ""
        job["ok"], job["done"] = ok, True
        if action == "update":
            refresh_source(svc, fetch=False)
        run_check(svc, force=True)
        if auto:
            return  # the auto-updater reports the outcome itself, after checking the service came back
        verb = {"start": "Started", "stop": "Stopped", "restart": "Restarted", "update": "Updated"}[action]
        from . import notify
        if ok:
            notify.alert("info", f"{verb} {svc.name} from the dashboard", f"Freed {freed} of old images." if freed else "",
                         service=svc.name, discord=False)
        else:
            notify.alert("error", f"{action.capitalize()} failed for {svc.name}",
                         "Started from the dashboard.", service=svc.name, log_lines=job["lines"][-15:])

    threading.Thread(target=work, name=f"job-{job['id']}", daemon=True).start()
    return job
