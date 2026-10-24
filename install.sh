#!/usr/bin/env bash
# Install or update pi-dash as a systemd service that runs as you. Safe to run again.
# Usage: ./install.sh      (from the folder you cloned pi-dash into; asks for sudo)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
CONF=/etc/pi-dash/config.toml

[ "$USER_NAME" != root ] || { echo "Run this as your normal user; it uses sudo where it needs to."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "pi-dash needs Python 3.11 or newer."; exit 1; }
command -v systemctl >/dev/null || { echo "pi-dash needs systemd."; exit 1; }

# Settings: created once from the example, then left alone.
if ! sudo test -f "$CONF"; then
  sudo install -d -m 755 /etc/pi-dash
  sudo install -m 600 -o "$USER_NAME" -g "$GROUP_NAME" "$DIR/config.example.toml" "$CONF"
  echo "Created $CONF"
  if [ -t 0 ]; then
    printf 'Discord webhook URL (hidden; Enter to skip): '; read -rs WEBHOOK; echo
    printf 'Your Discord user ID for pings (Enter to skip): '; read -r PING
    printf 'Heartbeat URL, e.g. from healthchecks.io, to hear when the Pi goes offline (Enter to skip): '; read -r HEARTBEAT
    WEBHOOK="$WEBHOOK" PING="$PING" HEARTBEAT="$HEARTBEAT" CONF="$CONF" python3 - <<'PY'
import json, os, re
path = os.environ["CONF"]
text = open(path).read()
for key, env in (("webhook_url", "WEBHOOK"), ("ping_user_id", "PING"), ("url", "HEARTBEAT")):
    if os.environ[env].strip():
        text = re.sub(rf'^{key} = ""', f"{key} = {json.dumps(os.environ[env].strip())}", text, count=1, flags=re.M)
open(path, "w").write(text)
PY
    printf 'Set a dashboard password? Recommended unless only you can reach this machine [y/N] '; read -r ANSWER
    case "$ANSWER" in [yY]*) (cd "$DIR" && PIDASH_CONFIG="$CONF" PIDASH_INSTALLING=1 python3 -m pidash.passwd) ;; esac
  fi
fi

# The assistant (optional): Claude in a chat panel. Its one dependency lives in .venv, away from the system Python.
PYTHON=/usr/bin/python3
assistant_on() { sudo python3 -c "import tomllib; print(tomllib.load(open('$CONF', 'rb')).get('assistant', {}).get('enabled', False))"; }
if [ "$(assistant_on)" != True ] && [ -t 0 ]; then
  printf 'Turn on the assistant? Claude looks into problems for you and asks before changing anything [y/N] '
  read -r ANSWER || ANSWER=n
  case "$ANSWER" in [yY]*)
    KEY="" BASE_URL=""
    if curl -fs -m 2 http://127.0.0.1:8787/health >/dev/null 2>&1; then
      printf 'cli-bridge answers on this machine. Use it (your Claude plan) instead of an API key? [Y/n] '
      read -r ANSWER || ANSWER=n
      case "$ANSWER" in [nN]*) ;; *) BASE_URL="http://127.0.0.1:8787" ;; esac
    fi
    if [ -z "$BASE_URL" ]; then
      printf 'Anthropic API key (hidden; Enter to add it to %s later): ' "$CONF"; read -rs KEY || KEY=""; echo
    fi
    KEY="$KEY" BASE_URL="$BASE_URL" CONF="$CONF" EXAMPLE="$DIR/config.example.toml" sudo -E python3 - <<'PY'
import json, os, re
path, key, base_url = os.environ["CONF"], os.environ["KEY"].strip(), os.environ["BASE_URL"]
text = open(path).read()
if "[assistant]" not in text:  # a config from before the assistant existed
    example = open(os.environ["EXAMPLE"]).read()
    text = text.rstrip() + "\n\n" + example[example.index("[assistant]"):]
head, section = text.split("[assistant]", 1)
section = re.sub(r"^enabled = \w+", "enabled = true", section, count=1, flags=re.M)
if key:
    section = re.sub(r'^api_key = ".*?"', f"api_key = {json.dumps(key)}", section, count=1, flags=re.M)
if base_url:
    if re.search(r"^base_url = ", section, flags=re.M):
        section = re.sub(r'^base_url = ".*?"', f"base_url = {json.dumps(base_url)}", section, count=1, flags=re.M)
    else:
        section = re.sub(r"^(api_key = .*)$", rf"\1\nbase_url = {json.dumps(base_url)}", section, count=1, flags=re.M)
open(path, "w").write(head + "[assistant]" + section)
PY
    ;;
  esac
fi
if [ "$(assistant_on)" = True ]; then
  if [ ! -x "$DIR/.venv/bin/python3" ]; then
    python3 -m venv --system-site-packages "$DIR/.venv" 2>/dev/null || {
      rm -rf "$DIR/.venv"; sudo apt-get install -y python3-venv >/dev/null
      python3 -m venv --system-site-packages "$DIR/.venv"; }
  fi
  "$DIR/.venv/bin/pip" install --quiet --disable-pip-version-check "anthropic>=1.17,<2"
  PYTHON="$DIR/.venv/bin/python3"
  sudo -n true 2>/dev/null || echo "Note: commands you approve that use sudo need passwordless sudo for $USER_NAME."
fi

# Only add groups that exist here; systemd refuses to start with an unknown one.
GROUPS_FOUND=""
for g in docker adm systemd-journal video; do
  getent group "$g" >/dev/null && GROUPS_FOUND="$GROUPS_FOUND $g"
done

fill() {
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@GROUP@|$GROUP_NAME|g" -e "s|@UID@|$(id -u)|g" -e "s|@DIR@|$DIR|g" \
      -e "s|@GROUPS@|${GROUPS_FOUND# }|g" -e "s|@PYTHON@|$PYTHON|g" "$1"
}

fill "$DIR/deploy/pi-dash.service" | sudo tee /etc/systemd/system/pi-dash.service >/dev/null
SUDOERS="$(mktemp)"  # private to this user, so nobody can swap it between the check and the install
trap 'rm -f "$SUDOERS"' EXIT
fill "$DIR/deploy/sudoers" > "$SUDOERS"
sudo visudo -cf "$SUDOERS" >/dev/null
sudo install -m 440 "$SUDOERS" /etc/sudoers.d/pi-dash

# Let the dashboard switch Wi-Fi networks (NetworkManager asks polkit; polkit picks the rule up by itself).
if command -v nmcli >/dev/null && sudo test -d /etc/polkit-1/rules.d; then
  fill "$DIR/deploy/polkit.rules" | sudo tee /etc/polkit-1/rules.d/50-pi-dash.rules >/dev/null
fi

# Keep your systemd --user services (kind "systemd-user") running when you're logged out.
sudo loginctl enable-linger "$USER_NAME"

sudo systemctl daemon-reload
sudo systemctl enable pi-dash >/dev/null 2>&1
sudo systemctl restart pi-dash
sleep 2
systemctl is-active --quiet pi-dash || { echo "pi-dash didn't start; see: journalctl -u pi-dash -n 50"; exit 1; }
echo "pi-dash is running: http://$(hostname):$(sudo python3 -c "import tomllib; print(tomllib.load(open('$CONF', 'rb')).get('server', {}).get('port', 9000))")"
[ -f "$HOME/services/services.json" ] || echo "Next: list your services in ~/services/services.json (see examples/services.example.json)."
