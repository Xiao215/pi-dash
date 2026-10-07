"""Discord alerts through a channel webhook. Every alert is also kept in the activity feed."""

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

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


def alert(level, title, detail="", service=None, fields=None, log_lines=None, discord=True):
    """Record an event and (unless discord=False) post it to Discord."""
    store.add_event(level, title, detail, service, log_lines)
    if discord and not muted():
        _outbox.put((level, title, detail, service, fields or [], log_lines or []))


def muted() -> bool:
    return store.load_state().get("muted_until", 0) > time.time()


def _embed(level, title, detail, service, fields, log_lines):
    emoji, colour = STYLE.get(level, STYLE["info"])
    description = detail
    if log_lines:
        tail = "\n".join(log_lines)[-1500:]
        description += f"\n```\n{tail.replace('```', '`​``')}\n```"
    embed = {
        "title": f"{emoji} {title}"[:256],
        "description": description[:4000],
        "color": colour,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
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
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)) + 0.5)
                continue
            log.error("Discord refused the alert: HTTP %s", e.code)
            return e.code < 500
        except OSError as e:  # offline: wait and try again
            log.warning("Discord unreachable (%s), retrying", e)
            time.sleep(min(60, 5 * 2 ** attempt))
    return False


def payload(item, ping_user=None) -> dict:
    ping_user = config.PING_USER if ping_user is None else ping_user
    body = {"username": config.HOSTNAME, "embeds": [_embed(*item)], "allowed_mentions": {"parse": []}}
    if item[0] == "error" and ping_user:  # mentions inside embeds don't ping, so put it in content
        body["content"] = f"<@{ping_user}>"
        body["allowed_mentions"] = {"users": [ping_user]}
    return body


def _sender():
    while True:
        item = _outbox.get()
        if not _post(payload(item)):
            time.sleep(60)
            _outbox.put(item)  # keep it until the network comes back


def start():
    threading.Thread(target=_sender, name="discord", daemon=True).start()
