"""What pi-dash remembers: the activity feed, minute-by-minute service samples, and small state."""

import json
import threading
import time
from collections import deque

from . import config

EVENTS = config.STATE_DIR / "events.jsonl"
SAMPLES = config.STATE_DIR / "samples.jsonl"
STATE = config.STATE_DIR / "state.json"

_lock = threading.Lock()


def _read_jsonl(path, since=0.0):
    out = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("t", 0) >= since:
                    out.append(row)
    except FileNotFoundError:
        pass
    return out


def _append(path, row):
    with _lock, path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def add_event(level, title, detail="", service=None, log_lines=None):
    """level: info | ok | warn | error | boot | digest"""
    row = {"t": time.time(), "level": level, "title": title, "detail": detail, "service": service}
    if log_lines:
        row["log"] = [l[:400] for l in log_lines[-30:]]
    _append(EVENTS, row)
    return row


def events(limit=100, since=0.0):
    return list(deque(_read_jsonl(EVENTS, since), maxlen=limit))[::-1]


def add_sample(states: dict[str, str], pi: dict | None = None):
    row = {"t": time.time(), "s": states}
    if pi:
        row["p"] = pi
    _append(SAMPLES, row)


def samples(since):
    return _read_jsonl(SAMPLES, since)


def prune():
    cutoff = time.time() - config.KEEP_DAYS * 86400
    for path in (EVENTS, SAMPLES):
        rows = _read_jsonl(path, cutoff)
        with _lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
            tmp.replace(path)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict):
    with _lock:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE)


def update_state(**changes):
    state = load_state()
    state.update(changes)
    save_state(state)
    return state
