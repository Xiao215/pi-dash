"""The Pi itself: CPU, memory, temperature, power, disk, network, pending OS updates."""

import json
import os
import shutil
import socket
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

# Interfaces whose traffic is counted elsewhere already (Tailscale rides on wlan0/eth0) or never leaves the Pi.
VIRTUAL = ("lo", "docker", "veth", "br-", "tailscale", "wg", "tun", "virbr", "cni", "flannel")

_live = {"cpu": 0.0, "rx": 0.0, "tx": 0.0}  # CPU %, network bytes/s over the last 5 s
_slow: dict = {}  # cached results of slower commands
_slow_at: dict = {}
updates: dict | None = None  # pending apt upgrades; refreshed by the health loop (apt is slow)


def run(cmd, timeout=10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_net_dev(text: str) -> tuple[int, int]:
    """/proc/net/dev -> total (received, sent) bytes over the real interfaces."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, data = line.partition(":")
        fields = data.split()
        if len(fields) < 9 or name.strip().startswith(VIRTUAL):
            continue
        rx += int(fields[0])
        tx += int(fields[8])
    return rx, tx


def net_totals() -> tuple[int, int]:
    try:
        return parse_net_dev(Path("/proc/net/dev").read_text())
    except OSError:
        return 0, 0


def _live_sampler():
    def cpu():
        parts = list(map(int, Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]))
        return sum(parts), parts[3] + parts[4]
    (total, idle), net, t = cpu(), net_totals(), time.monotonic()
    while True:
        time.sleep(5)
        (t2, i2), net2, now = cpu(), net_totals(), time.monotonic()
        if t2 > total:
            _live["cpu"] = round(100 * (1 - (i2 - idle) / (t2 - total)), 1)
        if net2[0] >= net[0] and net2[1] >= net[1]:  # counters reset when an interface goes away
            _live["rx"], _live["tx"] = ((b - a) / (now - t) for a, b in zip(net, net2))
        total, idle, net, t = t2, i2, net2, now


def start():
    threading.Thread(target=_live_sampler, name="live", daemon=True).start()


def online() -> bool:
    """Can the Pi reach the internet? Two well-known anycast addresses, so one outage doesn't count."""
    for addr in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            socket.create_connection(addr, timeout=3).close()
            return True
        except OSError:
            continue
    return False


def parse_upgradable(out: str) -> list[dict]:
    """`apt list --upgradable` -> [{"name", "security"}]. Lines look like
    openssl/stable-security 3.0.17-1~deb12u3 arm64 [upgradable from: 3.0.17-1~deb12u2]"""
    pkgs = []
    for line in out.splitlines():
        if "[upgradable from" not in line or "/" not in line:
            continue
        name, rest = line.split("/", 1)
        pkgs.append({"name": name, "security": "-security" in rest.split(" ", 1)[0]})
    return pkgs


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


_apt_seen = None


def refresh_updates():
    """What apt would upgrade. Uses the package lists the system's daily apt timer downloads; doesn't refresh them.
    apt takes a few seconds on a Pi, so it only asks again after the lists or the installed packages changed."""
    global updates, _apt_seen
    if not shutil.which("apt"):
        return
    seen = (_mtime("/var/lib/apt/lists"), _mtime("/var/lib/dpkg/status"))
    if updates is not None and seen == _apt_seen:
        return
    _apt_seen = seen
    pkgs = parse_upgradable(run(["apt", "list", "--upgradable"], timeout=120))
    checked = _mtime("/var/lib/apt/periodic/update-success-stamp") or seen[0]
    updates = {"count": len(pkgs), "security": sum(p["security"] for p in pkgs),
               "packages": [p["name"] for p in sorted(pkgs, key=lambda p: not p["security"])][:40], "checked": checked}


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
        if ts.get("BackendState"):  # no tailscale here: None, so the page can say it isn't set up
            tailscale = {"state": ts["BackendState"], "name": (ts.get("Self") or {}).get("DNSName", "").rstrip(".")}
    except ValueError:
        pass
    return {"addresses": addrs, "wifi": wifi, "tailscale": tailscale}


def _read(path) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _static():
    model = _read("/proc/device-tree/model").strip("\x00\n")  # Raspberry Pis and other boards with a device tree
    pretty = ""
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip('"')
    return {"model": model, "os": pretty, "kernel": os.uname().release}


def snapshot() -> dict:
    mem = meminfo()
    disk = shutil.disk_usage("/")
    value, flags = _cached("throttled", 30, throttled)
    return {
        **_cached("static", 86400, _static),
        "cpu": _live["cpu"],
        "load": os.getloadavg(),
        "cores": os.cpu_count(),
        "temp": temperature(),
        "mem": {"total": mem["MemTotal"], "available": mem["MemAvailable"]},
        "swap": {"total": mem.get("SwapTotal", 0), "free": mem.get("SwapFree", 0)},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "uptime": uptime(),
        "power": {"value": hex(value), "flags": flags},
        "reboot_required": Path("/var/run/reboot-required").exists(),
        "reboot_packages": _reboot_packages(),
        "network": _cached("network", 60, _network),
        "net": {"rx": round(_live["rx"]), "tx": round(_live["tx"])},
        "updates": updates,
    }


def _reboot_packages() -> list[str]:
    return sorted(set(_read("/var/run/reboot-required.pkgs").split()))
