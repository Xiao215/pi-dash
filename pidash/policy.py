"""What the assistant may run on its own and what waits for you, and keeping secrets out of what it reads.

A command runs without asking only when every part of it is on a short list of programs that just look:
reading files and logs, listing services, showing status. Anything else, or anything this can't be sure about,
is shown to you first. Being wrong in that direction costs a click; the other direction could cost the Pi."""

import re
import shlex
from pathlib import PurePosixPath

MAX_LENGTH = 4000


def _shell_tricks(cmd: str) -> bool:
    """$ (variables, $(...)), backticks and line breaks anywhere bash would act on them. Inside single quotes
    they're plain text, so `grep 'error$'` stays read-only."""
    quote, i = None, 0
    while i < len(cmd):
        c = cmd[i]
        if c in "\n\r":
            return True
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            i += 1
        elif c in "$`":
            return True
        elif c in "'\"":
            quote = None if quote == c else (quote or c)
        i += 1
    return False


def _split(cmd: str):
    """[[words], ...] per piped part, or None when the command uses anything but plain pipes (;, &&, >, ...)."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    parts, words = [], []
    for tok in lexer:
        if tok == "|":
            parts.append(words)
            words = []
        elif tok and set(tok) <= set("();<>|&"):
            return None
        else:
            words.append(tok)
    parts.append(words)
    return parts if all(parts) else None


def _short(words, letters: str) -> bool:
    """Any of these single-letter flags, alone or bundled (-fn, -xF)."""
    return any(w.startswith("-") and not w.startswith("--") and set(w[1:]) & set(letters) for w in words)


def _long(words, *names) -> bool:
    return any(w == n or w.startswith(n + "=") for w in words for n in names)


def _positional(words, takes_value=()):
    """The words that aren't options, skipping the values of options that take one."""
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w.startswith("-"):
            skip = w in takes_value
        else:
            out.append(w)
    return out


def _sub(words, takes_value=()):
    """The first word that isn't an option (the subcommand), and every word after it."""
    skip = False
    for i, w in enumerate(words):
        if skip:
            skip = False
        elif w.startswith("-"):
            skip = w in takes_value
        else:
            return w, words[i + 1:]
    return None, []


def _any(_):
    return True


def _tail(a):
    return not _short(a, "fF") and not _long(a, "--follow")


def _sort(a):
    return not _short(a, "o") and not _long(a, "--output", "--compress-program")


def _uniq(a):
    return len(_positional(a, ("-f", "-s", "-w"))) <= 1  # a second file name is where uniq writes


def _date(a):
    return not _short(a, "s") and not _long(a, "--set") and all(p.startswith("+") for p in _positional(a, ("-d", "-r")))


def _hostname(a):
    return not _positional(a)


def _ss(a):
    return not _short(a, "K") and not _long(a, "--kill")


def _dmesg(a):
    return not _short(a, "cCnDE") and not _long(a, "--clear", "--read-clear", "--console-level", "--console-off",
                                                "--console-on", "--follow", "-w")


def _top(a):
    return _short(a, "b")  # batch mode prints and exits; plain top needs a terminal


def _find(a):
    return not any(w in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")
                   for w in a)


def _journalctl(a):
    return not _short(a, "f") and not _long(a, "--follow", "--rotate", "--flush", "--sync", "--relinquish-var",
                                            "--smart-relinquish-var", "--setup-keys", "--update-catalog") \
        and not any(w.startswith("--vacuum") for w in a)


SYSTEMCTL_READ = {"status", "show", "cat", "is-active", "is-enabled", "is-failed", "is-system-running", "list-units",
                  "list-unit-files", "list-timers", "list-sockets", "list-dependencies", "list-jobs", "get-default"}


def _systemctl(a):
    sub, _ = _sub(a, ("-p", "--property", "-t", "--type", "--state", "-n", "--lines", "-o", "--output", "-H", "--host",
                      "-M", "--machine"))
    return sub is None or sub in SYSTEMCTL_READ


def _docker(a):
    opts = ("-H", "--host", "--context", "-c", "--config", "-l", "--log-level")
    sub, rest = _sub(a, opts)
    if sub in ("ps", "images", "inspect", "version", "info", "top", "port", "history"):
        return True
    if sub == "logs":
        return not _short(rest, "f") and not _long(rest, "--follow")
    if sub == "stats":
        return "--no-stream" in rest
    if sub == "system":
        return _positional(rest)[:1] == ["df"]
    if sub == "compose":
        csub, crest = _sub(rest, ("-f", "--file", "-p", "--project-name", "--project-directory", "--env-file",
                                  "--profile", "--ansi", "--progress"))
        if csub in ("ps", "ls", "images", "top", "version"):
            return True
        return csub == "logs" and not _short(crest, "f") and not _long(crest, "--follow")
    return False


def _apt(a):
    return _sub(a)[0] in ("list", "show", "policy", "search")


def _apt_cache(a):
    return _sub(a)[0] in ("policy", "show", "search", "depends", "rdepends", "madison", "showpkg", "stats")


def _dpkg(a):
    return all(w in ("-l", "--list", "-L", "--listfiles", "-s", "--status", "-S", "--search", "--print-architecture")
               or not w.startswith("-") for w in a) and any(w.startswith("-") for w in a)


def _vcgencmd(a):
    pos = _positional(a)
    return bool(pos) and (pos[0].startswith(("get_", "measure_")) or pos[0] in ("version", "commands"))


def _tailscale(a):
    sub, rest = _sub(a, ("--socket",))
    if sub in ("status", "ip", "version", "netcheck", "whois", "exit-node"):
        return sub != "exit-node" or _positional(rest)[:1] == ["list"]
    return sub in ("serve", "funnel") and _positional(rest) == ["status"]


NMCLI_OPTS = ("-f", "--fields", "-g", "--get-values", "-m", "--mode", "-c", "--colors", "-e", "--escape", "-w", "--wait")


def _nmcli(a):
    pos = _positional(a, NMCLI_OPTS)
    if not pos:
        return True
    obj, rest = pos[0], pos[1:]
    if "general".startswith(obj):
        return rest in ([], ["status"], ["permissions"])
    if "networking".startswith(obj):
        return rest in ([], ["connectivity"], ["connectivity", "check"])
    if "radio".startswith(obj):
        return rest in ([], ["all"], ["wifi"], ["wwan"])
    if "device".startswith(obj):
        return not rest or rest[0] in ("status", "show") or rest[:2] == ["wifi", "list"]
    if "connection".startswith(obj):
        return not rest or rest[0] == "show"
    return False


GIT_READ = {"log", "status", "diff", "show", "rev-parse", "describe", "ls-files", "blame", "shortlog", "reflog"}


def _git(a):
    if _long(a, "--output", "--ext-diff", "--exec-path", "--upload-pack", "--textconv") or "-c" in a:
        return False
    sub, rest = _sub(a, ("-C", "--git-dir", "--work-tree", "-n"))
    if sub in GIT_READ:
        return not (sub == "reflog" and _positional(rest)[:1] in (["expire"], ["delete"]))
    if sub in ("branch", "tag"):
        return not _positional(rest) and not _short(rest, "dDmMcC") and not _long(rest, "--delete", "--move", "--copy")
    return sub == "remote" and (_positional(rest) in ([], ["show"]) or _positional(rest)[:1] == ["show"])


def _timedatectl(a):
    return _sub(a)[0] in (None, "status", "show", "list-timezones", "timesync-status", "show-timesync")


def _hostnamectl(a):
    return _sub(a)[0] in (None, "status")


def _crontab(a):
    return a == ["-l"]


PROGRAMS = {
    **dict.fromkeys(["cat", "head", "wc", "grep", "egrep", "fgrep", "zgrep", "zcat", "ls", "stat", "df", "du",
                     "free", "uptime", "ps", "lsblk", "findmnt", "uname", "cut", "tr", "whoami", "id", "pwd", "echo",
                     "file", "which", "readlink", "realpath", "basename", "dirname", "nproc", "lscpu", "lsusb",
                     "lsof", "tac", "nl", "diff", "cmp", "md5sum", "sha256sum", "column", "vmstat", "sensors", "who",
                     "w", "last", "getent", "groups", "true", "seq", "jq", "rev"], _any),
    "tail": _tail, "sort": _sort, "uniq": _uniq, "date": _date, "hostname": _hostname, "ss": _ss, "dmesg": _dmesg,
    "top": _top, "find": _find, "journalctl": _journalctl, "systemctl": _systemctl, "docker": _docker,
    "apt": _apt, "apt-cache": _apt_cache, "dpkg": _dpkg, "dpkg-query": _dpkg, "vcgencmd": _vcgencmd,
    "tailscale": _tailscale, "nmcli": _nmcli, "git": _git, "timedatectl": _timedatectl, "hostnamectl": _hostnamectl,
    "crontab": _crontab,
    "ip": lambda a: _sub(a)[1][:1] in ([], ["show"], ["list"], ["ls"], ["get"]),
}


# ---- secrets ----

SECRET_FILES = re.compile(r"""(^|/)(\.env(\.[\w.-]+)?|[\w.-]+\.env|[\w.-]+\.(key|pem|p12|pfx)|id_[\w-]+|
    \.git-credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc|credentials(\.json)?|secrets?(\.\w+)?|shadow|gshadow)$
    |(^|/)\.ssh(/|$)|^/etc/pi-dash(/|$)|(^|/)\.docker/config\.json$|^/proc/[^/]+/environ$""", re.X)


def _secret_word(w: str) -> bool:
    w = w.split("=", 1)[-1] if w.startswith("--") else w
    return bool(w) and bool(SECRET_FILES.search(w.rstrip("/")))


def shows_secrets(parts) -> bool:
    for words in parts:
        if any(_secret_word(w) for w in words[1:]):
            return True
        prog, args = PurePosixPath(words[0]).name, words[1:]
        if prog == "docker" and _sub(args)[0] == "compose" and "config" in args:
            return True
        if prog == "nmcli" and (_short(args, "s") or _long(args, "--show-secrets")):
            return True
    return False


# ---- labels for a change ----

NETWORK = re.compile(r"\b(tailscale|tailscaled|nmcli|networkmanager|networking|wpa_supplicant|wpa_cli|dhcpcd|"
                     r"ufw|iptables|nft|netplan|ifdown|ifup|sshd?|rfkill)\b|\bip\s+(link|addr|address|route)\s+"
                     r"(set|add|del|delete|flush|change|replace)\b", re.I)
REBOOT = re.compile(r"\b(reboot|shutdown|halt|poweroff)\b", re.I)


def labels(cmd: str, parts=None) -> list[str]:
    """What the card says about a change, worked out here rather than taken from the model."""
    parts = parts if parts is not None else (_split(cmd) or [])
    out = []
    if re.search(r"(^|[\s;&|(])sudo\s", cmd):
        out.append("Runs as root")
    if REBOOT.search(cmd):
        out.append("Turns the Pi off" if re.search(r"\b(poweroff|halt)\b|shutdown\s+(-h|-P|--poweroff)", cmd)
                   else "Restarts the Pi")
    elif NETWORK.search(cmd):
        out.append("May cut your connection")
    if parts and shows_secrets(parts):
        out.append("May show secrets")
    return out


def classify(cmd: str) -> dict:
    """{"kind": "read" | "change" | "blocked", "labels": [...], "reason": "..."}. read runs at once; change waits."""
    if not cmd.strip():
        return {"kind": "blocked", "labels": [], "reason": "the command is empty"}
    if len(cmd) > MAX_LENGTH or "\0" in cmd:
        return {"kind": "blocked", "labels": [], "reason": f"commands are limited to {MAX_LENGTH} characters"}
    try:
        parts = _split(cmd)
        why = "" if parts is not None else "it chains, redirects or backgrounds commands"
    except ValueError:
        parts, why = None, "its quotes don't match up"

    def change(reason):
        return {"kind": "change", "labels": labels(cmd, parts or []), "reason": reason}
    if why:
        return change(why)
    if _shell_tricks(cmd):
        return change("it uses $, backticks or line breaks")
    if shows_secrets(parts):
        return change("it may show secrets")
    for words in parts:
        prog = words[0]
        if "=" in prog:
            return change("it sets environment variables")
        if "/" in prog and not re.fullmatch(r"/(usr/)?s?bin/[\w.+-]+", prog):
            return change(f"{prog} isn't a system program")
        rule = PROGRAMS.get(PurePosixPath(prog).name)
        if not rule or not rule(words[1:]):
            return change(f"{PurePosixPath(prog).name} {'can change things' if rule else 'is not on the read-only list'}")
    return {"kind": "read", "labels": [], "reason": ""}


# ---- scrubbing secrets out of what the assistant reads ----

HIDDEN = "[hidden]"
REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
     "-----BEGIN PRIVATE KEY----- [hidden] -----END PRIVATE KEY-----"),
    (re.compile(r"(https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+"), rf"\1{HIDDEN}"),
    (re.compile(r"\bsk-ant-[\w-]{8,}"), f"sk-ant-{HIDDEN}"),
    (re.compile(r"\b(gh[pousr]_|github_pat_)\w{20,}"), rf"\1{HIDDEN}"),
    (re.compile(r"\btskey-[\w-]{8,}"), f"tskey-{HIDDEN}"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), HIDDEN),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), HIDDEN),  # JWTs
    (re.compile(r"\b[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}"), HIDDEN),  # Discord bot tokens
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic|token)\s+)\S+"), rf"\1{HIDDEN}"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), rf"\1{HIDDEN}@"),  # passwords in URLs
    (re.compile(r"""(?ix)\b([\w.-]*(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|access[_-]?key|
                    private[_-]?key|client[_-]?secret|webhook(?:[_-]?url)?|auth[_-]?token|credentials?)[\w.-]*)
                    (["']?\s*[=:]\s*["']?)(?!\[hidden\])([^\s"',;]{4,})"""), rf"\1\2{HIDDEN}"),
]


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text
