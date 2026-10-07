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

WEBHOOK_URL = _get("discord", "webhook_url", "")
PING_USER = str(_get("discord", "ping_user_id", ""))

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


def load_services() -> list[Service]:
    try:
        raw = json.loads(REGISTRY.read_text())
    except FileNotFoundError:
        return []
    known = Service.__dataclass_fields__
    return [Service(**{k: v for k, v in s.items() if k in known}) for s in raw.get("services", [])]
