"""Optional dashboard password: a scrypt hash in the config, 30-day session cookies, and a lockout on guessing."""

import base64
import hashlib
import hmac
import secrets
import threading
import time

from . import store

COOKIE = "pidash_session"
SESSION_DAYS = 30
MAX_FAILURES = 5          # wrong passwords per address ...
FAILURE_WINDOW = 300      # ... within this many seconds, then locked until the window passes

_N, _R, _P = 2 ** 14, 8, 1
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()  # _failures


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


# ---- sessions (only a hash of each token is stored, so the state file can't be used to log in) ----

def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with store.edit_state() as state:
        sessions = {k: exp for k, exp in state.get("sessions", {}).items() if exp > now}
        sessions[_key(token)] = now + SESSION_DAYS * 86400
        state["sessions"] = sessions
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    return store.load_state().get("sessions", {}).get(_key(token), 0) > time.time()


def end_session(token: str | None):
    if not token:
        return
    with store.edit_state() as state:
        state.get("sessions", {}).pop(_key(token), None)


# ---- guessing ----------------------------------------------------------------------------------

def locked_out(addr: str) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _failures.pop(addr, []) if now - t < FAILURE_WINDOW]
        if recent:  # forget addresses whose failures have all expired
            _failures[addr] = recent
        return len(recent) >= MAX_FAILURES


def record_failure(addr: str):
    with _lock:
        _failures.setdefault(addr, []).append(time.time())


def clear_failures(addr: str):
    with _lock:
        _failures.pop(addr, None)
