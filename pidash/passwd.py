"""Set, change or remove the dashboard password:  python3 -m pidash.passwd  [--remove]

Writes a scrypt hash (never the password) to password_hash in the config file, then restart pi-dash."""

import getpass
import os
import re
import sys

from . import auth, config


def write_hash(value: str) -> None:
    text = config.CONFIG_FILE.read_text() if config.CONFIG_FILE.exists() else "[server]\n"
    line = f'password_hash = "{value}"'
    if re.search(r"^password_hash\s*=.*$", text, flags=re.M):
        text = re.sub(r"^password_hash\s*=.*$", line, text, count=1, flags=re.M)
    elif re.search(r"^\[server\]\s*$", text, flags=re.M):
        text = re.sub(r"^\[server\]\s*$", "[server]\n" + line, text, count=1, flags=re.M)
    else:
        text = "[server]\n" + line + "\n\n" + text
    config.CONFIG_FILE.write_text(text)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if "--remove" in argv:
            write_hash("")
            print(f"Password removed from {config.CONFIG_FILE}.")
        else:
            first = getpass.getpass("New dashboard password: ")
            if len(first) < 8:
                print("Use at least 8 characters.")
                return 1
            if getpass.getpass("Again: ") != first:
                print("The two don't match; nothing changed.")
                return 1
            write_hash(auth.hash_password(first))
            print(f"Saved (as a hash) in {config.CONFIG_FILE}.")
    except PermissionError:
        print(f"Can't write {config.CONFIG_FILE}. Run this as the user pi-dash runs as.")
        return 1
    if not os.environ.get("PIDASH_INSTALLING"):  # install.sh restarts pi-dash itself
        print("Apply it with: sudo systemctl restart pi-dash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
