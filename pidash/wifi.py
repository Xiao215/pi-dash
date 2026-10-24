"""Wi-Fi through NetworkManager: the networks in range, and switching to one with a way back.

Switching drops the Pi off the old network, and with it the page that asked. So the switch runs here, on its own
thread: join, check the internet works, and if it doesn't, go back to the network the Pi was on. The page reads
how it went from /api/state once it can reach the Pi again."""

import logging
import subprocess
import threading
import time

from . import notify, services, system
from .system import fields

log = logging.getLogger(__name__)

JOIN_WAIT = 45     # s nmcli waits for the new network to connect
ONLINE_WAIT = 30   # s after joining for the internet to work before going back

switch: dict | None = None  # the last switch: {"ssid", "previous", "state", "ok", "message", "started", "finished"}
_lock = threading.Lock()


def nmcli(*args, timeout=15) -> tuple[int, str]:
    """(exit code, stdout + stderr) of nmcli in terse mode."""
    try:
        p = subprocess.run(["nmcli", "-t", *args], capture_output=True, text=True, timeout=timeout, env=services.ENV)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def parse_scan(text: str, saved: dict) -> list[dict]:
    """`nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list` -> one row per network name, strongest first.
    Access points that share a name (a mesh, 2.4 and 5 GHz) are one network; hidden ones (no name) are left out."""
    nets: dict[str, dict] = {}
    for line in text.splitlines():
        f = fields(line)
        if len(f) != 4 or not f[1]:
            continue
        in_use, ssid, signal, security = f
        security = "" if security in ("", "--") else security
        n = nets.setdefault(ssid, {"ssid": ssid, "signal": 0, "security": security, "active": False,
                                   "saved": ssid in saved})
        n["active"] |= in_use == "*"
        n["signal"] = max(n["signal"], int(signal or 0))
    return sorted(nets.values(), key=lambda n: (not n["active"], -n["signal"], n["ssid"].lower()))


def device() -> str:
    """The Wi-Fi interface (usually wlan0), or "" when there's none."""
    _, out = nmcli("-f", "DEVICE,TYPE", "device")
    return next((f[0] for f in map(fields, out.splitlines()) if len(f) == 2 and f[1] == "wifi"), "")


def saved_networks() -> dict[str, str]:
    """Network name -> the saved connection's UUID, for every Wi-Fi network the Pi has joined before."""
    _, out = nmcli("-f", "UUID,TYPE", "connection", "show")
    saved = {}
    for f in map(fields, out.splitlines()):
        if len(f) == 2 and f[1] == "802-11-wireless":
            code, ssid = nmcli("-g", "802-11-wireless.ssid", "connection", "show", "uuid", f[0])
            if code == 0 and ssid:
                saved.setdefault(ssid, f[0])
    return saved


def active(dev: str) -> tuple[str, str]:
    """(UUID, network name) of the connection on the Wi-Fi interface, or ("", "") when it isn't connected."""
    _, out = nmcli("-f", "UUID,DEVICE", "connection", "show", "--active")
    for f in map(fields, out.splitlines()):
        if len(f) == 2 and f[1] == dev:
            code, ssid = nmcli("-g", "802-11-wireless.ssid", "connection", "show", "uuid", f[0])
            return f[0], ssid if code == 0 else ""
    return "", ""


def networks() -> dict:
    """The networks in range, asking the radio for a fresh scan first (a few seconds)."""
    dev = device()
    if not dev:
        return {"device": "", "networks": []}
    saved = saved_networks()
    code, out = nmcli("-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", dev, "--rescan", "yes",
                      timeout=30)
    return {"device": dev, "networks": parse_scan(out if code == 0 else "", saved)}


def reason(out: str) -> str:
    """nmcli's error, in words for the page."""
    low = out.lower()
    if any(s in low for s in ("secrets were required", "passwords or encryption keys", "psk: property is invalid",
                              "802.1x supplicant")):
        return "Wrong password"
    if "no network with ssid" in low:
        return "Out of range"
    if "timeout" in low or "timed out" in low:
        return "Took too long to connect"
    if "not authorized" in low:
        return "pi-dash isn't allowed to change Wi-Fi; run install.sh again"
    lines = out.splitlines()
    line = next((x for x in reversed(lines) if x.startswith("Error")), lines[-1] if lines else "")
    return line.removeprefix("Error: ") or "nmcli failed"


def start(ssid: str, password: str = "", username: str = "") -> dict:
    """Begin switching to `ssid`; the work goes on in the background. Raises ValueError when it can't start.
    A username means an enterprise (802.1X) network, signed in to with it and the password."""
    global switch
    with _lock:
        if switch and switch["state"] != "done":
            raise ValueError(f"Already switching to {switch['ssid']}")
        dev = device()
        if not dev:
            raise ValueError("This machine has no Wi-Fi")
        prev_uuid, prev_name = active(dev)
        if prev_uuid and prev_name == ssid:
            raise ValueError(f"Already on {ssid}")
        switch = {"ssid": ssid, "previous": prev_name, "state": "joining", "ok": None, "message": "",
                  "started": time.time(), "finished": None}
        job = switch
    threading.Thread(target=_run, args=(job, dev, ssid, password, prev_uuid, prev_name, username), name="wifi",
                     daemon=True).start()
    return job


def _run(job, dev, ssid, password, prev_uuid, prev_name, username=""):
    try:
        _switch(job, dev, ssid, password, prev_uuid, prev_name, username)
    except Exception as e:  # noqa: BLE001 - finish, or the page would wait on this switch forever
        log.exception("wifi switch")
        job.update(ok=False, message=f"pi-dash: {e}")
    job.update(state="done", finished=time.time())
    system.forget("network")  # show the new network on the next refresh, not a minute later
    if job["ok"]:
        notify.alert("info", f"Switched Wi-Fi to {ssid} from the dashboard",
                     f"Was on {prev_name}." if prev_name else "", discord=False)
    else:
        notify.alert("warn", f"Couldn't switch Wi-Fi to {ssid}", job["message"])


def _switch(job, dev, ssid, password, prev_uuid, prev_name, username=""):
    saved = saved_networks()
    new_profile = ssid not in saved
    code, out = 0, ""
    # Passwords are on nmcli's command line for a few seconds; on a one-person Pi only you can see that.
    if new_profile and username:
        # Enterprise: a profile with the sign-in first. PEAP or TTLS with MSCHAPv2 is what most of them use
        # (eduroam, offices). The Pi has no CA certificate to check the network against; the page says so.
        code, out = nmcli("connection", "add", "type", "wifi", "ifname", dev, "con-name", ssid, "ssid", ssid,
                          "wifi-sec.key-mgmt", "wpa-eap", "802-1x.eap", "peap,ttls", "802-1x.phase2-auth", "mschapv2",
                          "802-1x.identity", username, "802-1x.password", password)
        cmd = ["connection", "up", "uuid", saved_networks().get(ssid, ""), "ifname", dev]
    elif new_profile:
        cmd = ["device", "wifi", "connect", ssid, *(["password", password] if password else []), "ifname", dev]
    else:
        cmd = ["connection", "up", "uuid", saved[ssid], "ifname", dev]
    if code == 0:
        code, out = nmcli("--wait", str(JOIN_WAIT), *cmd, timeout=JOIN_WAIT + 15)
    if code == 0:
        job["state"] = "checking"
        deadline = time.monotonic() + ONLINE_WAIT
        while time.monotonic() < deadline:
            if system.online():
                job.update(ok=True, message=f"Connected to {ssid}")
                return
            time.sleep(3)
        problem = f"{ssid} has no internet"
    else:
        problem = reason(out)
        if username and problem == "Wrong password":
            problem = "Wrong username or password"
    job["state"] = "going-back"
    if new_profile:  # don't leave a profile with a wrong password behind for NetworkManager to retry
        new_uuid = saved_networks().get(ssid)
        if new_uuid:
            nmcli("connection", "delete", "uuid", new_uuid)
    if prev_uuid:
        if active(dev)[0] != prev_uuid:
            back, out = nmcli("--wait", str(JOIN_WAIT), "connection", "up", "uuid", prev_uuid, "ifname", dev,
                              timeout=JOIN_WAIT + 15)
            if back != 0:
                job.update(ok=False, message=f"{problem}, and going back to {prev_name} failed too: {reason(out)}")
                return
        job.update(ok=False, message=f"{problem}, so the Pi went back to {prev_name}")
    else:
        nmcli("device", "disconnect", dev)
        job.update(ok=False, message=problem)
