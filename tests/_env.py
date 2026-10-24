"""Points pi-dash at a throwaway config and state folder. Every test module imports this before pidash."""

import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
TMP = Path(_tmp.name)
CONFIG = TMP / "config.toml"
CONFIG.write_text("""
[server]
port = 9123
allowed_hosts = ["dash.example.org"]
[services]
registry = "%s/services.json"
extra_path = ["~/tools/bin"]
[discord]
webhook_url = "https://discord.com/api/webhooks/1/abc"
ping_user_id = "42"
[assistant]
enabled = true
api_key = "sk-ant-test"
daily_budget_usd = 2
""" % _tmp.name)
os.environ["PIDASH_CONFIG"] = str(CONFIG)
os.environ["STATE_DIRECTORY"] = str(TMP / "state")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
