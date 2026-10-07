"""Settings from /etc/pi-dash/config.toml (see config.example.toml), and the service registry."""

import json
import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("PIDASH_CONFIG", "/etc/pi-dash/config.toml"))


def _load() -> dict:
    try:
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


_cfg = _load()


def _get(section, key, default):
    return _cfg.get(section, {}).get(key, default)


HOSTNAME = socket.gethostname()

HOST = _get("server", "host", "0.0.0.0")
PORT = int(_get("server", "port", 9000))
ALLOWED_HOSTS = [h.lower() for h in _get("server", "allowed_hosts", [])]
PASSWORD_HASH = _get("server", "password_hash", "")  # set with: python3 -m pidash.passwd

REGISTRY = Path(_get("services", "registry", "~/services/services.json")).expanduser()
EXTRA_PATH = [str(Path(p).expanduser()) for p in _get("services", "extra_path", ["~/.local/bin"])]
PRUNE_IMAGES = bool(_get("services", "prune_images", True))  # remove the old images a Docker update leaves behind
BUILD_CACHE_GB = float(_get("services", "build_cache_gb", 1))  # Docker build cache to keep (newest first); 0 keeps none

WEBHOOK_URL = _get("discord", "webhook_url", "")
PING_USER = str(_get("discord", "ping_user_id", ""))

# An outside service (e.g. healthchecks.io) that pages you when these pings stop, i.e. when the Pi is off or offline.
HEARTBEAT_URL = _get("heartbeat", "url", "")
HEARTBEAT_EVERY = int(_get("heartbeat", "every", 120))

DIGEST_HOUR = int(_get("schedule", "digest_hour", 9))       # -1 turns the morning summary off
UPDATE_EVERY = int(_get("schedule", "update_every", 300))   # new commits / images
HEALTH_EVERY = int(_get("schedule", "health_every", 600))   # disk, temperature, power, failed units
SAMPLE_EVERY = 60                                            # one status sample per minute
ERROR_COOLDOWN = int(_get("schedule", "error_cooldown", 600))
KEEP_DAYS = int(_get("schedule", "keep_days", 7))

DISK_WARN = float(_get("thresholds", "disk_percent", 80))
TEMP_WARN = float(_get("thresholds", "temperature_c", 75))
MEM_FREE_WARN = float(_get("thresholds", "memory_free_percent", 10))

STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", "~/.local/state/pi-dash")).expanduser()
STATE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Service:
    name: str
    kind: str                       # "compose" | "systemd-user" | "systemd"
    description: str = ""
    dir: str = ""
    unit: str = ""                  # systemd unit
    container: str = ""             # compose: the container to watch
    health: str = ""                # URL that answers 2xx when healthy
    update: list[str] = field(default_factory=list)  # shell steps run in `dir`
    url: str = ""                   # optional link shown on the card
    repo: str = ""                  # git checkout, relative to dir (default: dir itself)
    check: dict = field(default_factory=dict)  # {"command": ..., "problem": ..., "every": 300}: must exit 0

    @property
    def path(self) -> Path:
        return Path(self.dir).expanduser()

    @property
    def repo_path(self) -> Path:
        return self.path / self.repo if self.repo else self.path


def seconds(value) -> int:
    """300, "300", "45s", "30m", "6h", "1d" -> seconds."""
    text = str(value).strip().lower()
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(text[-1:])
    return int(float(text[:-1]) * unit) if unit else int(float(text))


@dataclass
class Job:
    """Something that runs on a schedule: a systemd timer, or anything that pings /api/ping/<name> when done."""
    name: str
    description: str = ""
    timer: str = ""                 # systemd timer, e.g. "backup.timer"
    user: bool = False              # a `systemctl --user` timer
    service: str = ""               # the unit the timer starts (default: the timer's name with .service)
    every: int = 0                  # expected interval in seconds (from "6h", "1d", ...); 0: don't watch for missed runs
    grace: int = 0                  # how late a run may be before it counts as missed (default: 10%, at least 10 min)

    def __post_init__(self):
        self.every = seconds(self.every) if self.every else 0
        self.grace = seconds(self.grace) if self.grace else max(600, self.every // 10)
        if self.timer and not self.service:
            self.service = self.timer.removesuffix(".timer") + ".service"


def _registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except FileNotFoundError:
        return {}


def load_services() -> list[Service]:
    known = Service.__dataclass_fields__
    return [Service(**{k: v for k, v in s.items() if k in known}) for s in _registry().get("services", [])]


def load_jobs() -> list[Job]:
    known = Job.__dataclass_fields__
    return [Job(**{k: v for k, v in j.items() if k in known}) for j in _registry().get("jobs", [])]
