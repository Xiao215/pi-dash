#!/usr/bin/env bash
# Install or update pi-dash as a systemd service that runs as you. Safe to run again.
# Usage: ./install.sh      (from the folder you cloned pi-dash into; asks for sudo)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
CONF=/etc/pi-dash/config.toml

[ "$USER_NAME" != root ] || { echo "Run this as your normal user; it uses sudo where it needs to."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "pi-dash needs Python 3.11 or newer."; exit 1; }
command -v systemctl >/dev/null || { echo "pi-dash needs systemd."; exit 1; }

# Settings: created once from the example, then left alone.
if ! sudo test -f "$CONF"; then
  sudo install -d -m 755 /etc/pi-dash
  sudo install -m 600 -o "$USER_NAME" -g "$(id -gn)" "$DIR/config.example.toml" "$CONF"
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

# Only add groups that exist here; systemd refuses to start with an unknown one.
GROUPS_FOUND=""
for g in docker adm systemd-journal video; do
  getent group "$g" >/dev/null && GROUPS_FOUND="$GROUPS_FOUND $g"
done

fill() {
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@UID@|$(id -u)|g" -e "s|@DIR@|$DIR|g" -e "s|@GROUPS@|${GROUPS_FOUND# }|g" "$1"
}

fill "$DIR/deploy/pi-dash.service" | sudo tee /etc/systemd/system/pi-dash.service >/dev/null
fill "$DIR/deploy/sudoers" > /tmp/pi-dash.sudoers
sudo visudo -cf /tmp/pi-dash.sudoers >/dev/null
sudo install -m 440 /tmp/pi-dash.sudoers /etc/sudoers.d/pi-dash
rm -f /tmp/pi-dash.sudoers

# Keep your systemd --user services (kind "systemd-user") running when you're logged out.
sudo loginctl enable-linger "$USER_NAME"

sudo systemctl daemon-reload
sudo systemctl enable pi-dash >/dev/null 2>&1
sudo systemctl restart pi-dash
sleep 2
systemctl is-active --quiet pi-dash || { echo "pi-dash didn't start; see: journalctl -u pi-dash -n 50"; exit 1; }
echo "pi-dash is running: http://$(hostname):$(sudo python3 -c "import tomllib; print(tomllib.load(open('$CONF', 'rb')).get('server', {}).get('port', 9000))")"
[ -f "$HOME/services/services.json" ] || echo "Next: list your services in ~/services/services.json (see examples/services.example.json)."
