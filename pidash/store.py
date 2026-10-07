"""What pi-dash remembers: the activity feed, minute-by-minute service samples, and small state."""

import bisect
import json
import os
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


def add_sample(states: dict[str, str], pi: dict | None = None, res: dict | None = None):
    """res: per service [cpu %, memory MB, health check ms], None where unknown."""
    row = {"t": time.time(), "s": states}
    if pi:
        row["p"] = pi
    if res:
        row["r"] = res
    _append(SAMPLES, row)


# The page asks for the last 24 h every few seconds, so keep those rows parsed in memory and only read
# what was appended since. Older ranges (the 7-day charts) read the file.
CACHE_SPAN = 25 * 3600
_cache = {"rows": [], "pos": 0, "id": None}


def _sync_cache():
    try:
        with SAMPLES.open("rb") as f:
            st = os.fstat(f.fileno())
            ident = (st.st_dev, st.st_ino, f.read(64))  # the head too: a rewritten file may reuse the inode
            if ident != _cache["id"] or st.st_size < _cache["pos"]:
                _cache.update(rows=[], pos=0, id=ident)
            f.seek(_cache["pos"])
            data = f.read()
    except FileNotFoundError:
        _cache.update(rows=[], pos=0, id=None)
        return
    end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
    _cache["pos"] += end
    cutoff = time.time() - CACHE_SPAN
    rows = _cache["rows"]
    for line in data[:end].splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("t", 0) >= cutoff:
            rows.append(row)
    drop = bisect.bisect_left(rows, cutoff, key=lambda r: r["t"])
    if drop:
        del rows[:drop]


def samples(since):
    if since < time.time() - CACHE_SPAN + 60:
        return _read_jsonl(SAMPLES, since)
    with _lock:
        _sync_cache()
        rows = _cache["rows"]
        return rows[bisect.bisect_left(rows, since, key=lambda r: r["t"]):]


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
