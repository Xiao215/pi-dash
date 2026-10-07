"""The Pi itself: CPU, memory, temperature, power, disk, network."""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

THROTTLE_BITS = {  # vcgencmd get_throttled
    0: "under-voltage now",
    1: "CPU speed capped now",
    2: "throttled now",
    3: "temperature limit now",
    16: "under-voltage since boot",
    17: "CPU speed capped since boot",
    18: "throttled since boot",
    19: "temperature limit since boot",
}

_cpu = {"pct": 0.0}
_slow: dict = {}  # cached results of slower commands
_slow_at: dict = {}


def run(cmd, timeout=10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _cpu_sampler():
    def read():
        parts = list(map(int, Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]))
        idle = parts[3] + parts[4]
        return sum(parts), idle
    total, idle = read()
    while True:
        time.sleep(5)
        t2, i2 = read()
        if t2 > total:
            _cpu["pct"] = round(100 * (1 - (i2 - idle) / (t2 - total)), 1)
        total, idle = t2, i2


def start():
    threading.Thread(target=_cpu_sampler, name="cpu", daemon=True).start()


def _cached(key, ttl, fn):
    if time.time() - _slow_at.get(key, 0) > ttl:
        _slow[key] = fn()
        _slow_at[key] = time.time()
    return _slow[key]


def meminfo():
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        info[k] = int(v.split()[0]) * 1024
    return info


def temperature() -> float | None:
    """CPU temperature in °C, or None on machines without a sensor (many VMs)."""
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def parse_throttled(out: str) -> tuple[int, list[str]]:
    """`throttled=0x50005` -> (0x50005, ["under-voltage now", ...])."""
    try:
        value = int(out.strip().split("=")[1], 16)
    except (IndexError, ValueError):
        return 0, []
    return value, [text for bit, text in THROTTLE_BITS.items() if value & (1 << bit)]


def throttled() -> tuple[int, list[str]]:
    return parse_throttled(run(["vcgencmd", "get_throttled"]))  # not a Pi: no vcgencmd, no flags


def uptime() -> float:
    return float(Path("/proc/uptime").read_text().split()[0])


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _network():
    addrs = {}
    try:
        for iface in json.loads(run(["ip", "-j", "-4", "addr"]) or "[]"):
            for a in iface.get("addr_info", []):
                if iface["ifname"] != "lo":
                    addrs[iface["ifname"]] = a["local"]
    except ValueError:
        pass
    wifi = None
    for line in run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"]).splitlines():
        active, *rest = line.split(":")
        if active == "yes":
            wifi = {"ssid": ":".join(rest[:-1]), "signal": int(rest[-1] or 0)}
    tailscale = None
    try:
        ts = json.loads(run(["tailscale", "status", "--json"]) or "{}")
        tailscale = {"state": ts.get("BackendState"),
                     "name": (ts.get("Self") or {}).get("DNSName", "").rstrip(".")}
    except ValueError:
        pass
    return {"addresses": addrs, "wifi": wifi, "tailscale": tailscale}


def _static():
    model = Path("/proc/device-tree/model").read_text().strip("\x00\n") if Path("/proc/device-tree/model").exists() else ""
    pretty = ""
    for line in Path("/etc/os-release").read_text().splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip('"')
    return {"model": model, "os": pretty, "kernel": os.uname().release}


def snapshot() -> dict:
    mem = meminfo()
    disk = shutil.disk_usage("/")
    value, flags = _cached("throttled", 30, throttled)
    return {
        **_cached("static", 86400, _static),
        "cpu": _cpu["pct"],
        "load": os.getloadavg(),
        "cores": os.cpu_count(),
        "temp": temperature(),
        "mem": {"total": mem["MemTotal"], "available": mem["MemAvailable"]},
        "swap": {"total": mem.get("SwapTotal", 0), "free": mem.get("SwapFree", 0)},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "uptime": uptime(),
        "power": {"value": hex(value), "flags": flags},
        "reboot_required": Path("/var/run/reboot-required").exists(),
        "network": _cached("network", 60, _network),
    }
