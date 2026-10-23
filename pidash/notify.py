"""Discord alerts through a channel webhook. Every alert is also kept in the activity feed."""

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import datetime

from . import config, store

log = logging.getLogger(__name__)

STYLE = {  # level -> (emoji, embed colour)
    "error": ("🔴", 0xE5484D),
    "warn": ("🟠", 0xF5A524),
    "ok": ("✅", 0x30A46C),
    "boot": ("🔄", 0x3E63DD),
    "info": ("ℹ️", 0x8B8D98),
    "digest": ("☀️", 0x3E63DD),
    "update": ("⬆️", 0x3E63DD),
}

_outbox: queue.Queue = queue.Queue()

# One alert on its way to Discord. `at` is time.monotonic() when it happened: a Pi that starts without internet
# runs on the last time it saved until it reaches a time server, so the wall clock is only read when sending.
# Alerts with the same `group` (the title unless given) are sent once when they piled up while offline.
Alert = namedtuple("Alert", "level title detail service fields log_lines at group repeats", defaults=(None, None, 0))


def alert(level, title, detail="", service=None, fields=None, log_lines=None, discord=True, group=None):
    """Record an event and (unless discord=False) post it to Discord."""
    store.add_event(level, title, detail, service, log_lines)
    if discord and not muted():
        _outbox.put(Alert(level, title, detail, service, fields or [], log_lines or [], time.monotonic(), group))


def ago(seconds) -> str:
    """A duration in words for alert text: "45 s", "12 min", "3 h 5 min", "4 days"."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} days"


def when(t) -> str:
    """A moment in alert text. Discord shows it in the reader's own time zone, with the date when it's not today."""
    return f"<t:{int(t)}:{'t' if time.time() - t < 20 * 3600 else 'f'}>"


def wall(at) -> float:
    """The wall-clock time of a time.monotonic() reading."""
    return time.time() - (time.monotonic() - at)


def muted() -> bool:
    return store.load_state().get("muted_until", 0) > time.time()


def _embed(item: Alert):
    level, title, detail, service, fields, log_lines, at = item[:7]
    emoji, colour = STYLE.get(level, STYLE["info"])
    description = detail
    if item.repeats:
        description += f"\n\n+{item.repeats} more like this, held while Discord couldn't be reached."
    if log_lines:
        tail = "\n".join(log_lines)[-1500:]
        description += f"\n```\n{tail.replace('```', '`​``')}\n```"
    embed = {
        "title": f"{emoji} {title}"[:256],
        "description": description[:4000],
        "color": colour,
        "timestamp": datetime.fromtimestamp(wall(at) if at else time.time()).astimezone().isoformat(timespec="seconds"),
        "footer": {"text": config.HOSTNAME + (f" · {service}" if service else "")},
    }
    if fields:
        embed["fields"] = [{"name": n[:256], "value": (v or "—")[:1024], "inline": inline}
                           for n, v, inline in fields][:25]
    return embed


def _post(payload) -> bool:
    url = config.WEBHOOK_URL
    if not url:
        return True  # no webhook configured: alerts stay on the dashboard
    data = json.dumps(payload).encode()
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "User-Agent": "pi-dash"})
        try:
            urllib.request.urlopen(req, timeout=15).close()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(_retry_after(e) + 0.5)
                continue
            log.error("Discord refused the alert: HTTP %s", e.code)
            e.close()
            return e.code < 500
        except OSError as e:  # offline: wait and try again
            log.warning("Discord unreachable (%s), retrying", e)
            time.sleep(min(60, 5 * 2 ** attempt))
    return False


def _retry_after(e: urllib.error.HTTPError) -> float:
    """Seconds Discord asks to wait. Its JSON says so; a rate limit from Cloudflare in front of it is HTML."""
    try:
        return min(300.0, float(json.loads(e.read()).get("retry_after", 2)))
    except (ValueError, TypeError, AttributeError, OSError):
        return 5.0
    finally:
        e.close()


def payload(item, ping_user=None) -> dict:
    item = Alert(*item)
    ping_user = config.PING_USER if ping_user is None else ping_user
    body = {"username": config.HOSTNAME, "embeds": [_embed(item)], "allowed_mentions": {"parse": []}}
    if item.level == "error" and ping_user:  # mentions inside embeds don't ping, so put it in content
        body["content"] = f"<@{ping_user}>"
        body["allowed_mentions"] = {"users": [ping_user]}
    return body


def collapse(held: list[Alert]) -> list[Alert]:
    """Alerts that piled up while Discord couldn't be reached: one per group, in the order the groups began,
    each the latest of its group (so "keeps crashing" says how many), counting the ones it stands for."""
    groups: dict = {}  # (service, group) -> [latest alert, how many others it stands for]
    for item in held:
        g = groups.get((item.service, item.group or item.title))
        if g:
            g[0], g[1] = item, g[1] + 1 + item.repeats
        else:
            groups[(item.service, item.group or item.title)] = [item, item.repeats]
    return [item._replace(repeats=n) for item, n in groups.values()]


def _send(item) -> bool:
    """Post one alert. False while Discord can't be reached; an alert Discord can't take is dropped."""
    try:
        return _post(payload(item))
    except Exception:  # noqa: BLE001 - drop this one alert rather than stop sending
        log.exception("sending an alert failed")
        return True


def _drain() -> list:
    items = []
    while True:
        try:
            items.append(_outbox.get_nowait())
        except queue.Empty:
            return items


def _sender():
    held: list[Alert] = []  # couldn't be sent yet, oldest first
    while True:
        if held:
            time.sleep(60)
            held = collapse(held + _drain())
        else:
            held = [_outbox.get()]
        while held and _send(held[0]):
            held.pop(0)


def start():
    threading.Thread(target=_sender, name="discord", daemon=True).start()
