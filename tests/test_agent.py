"""Tests for the assistant: what runs without asking, secret scrubbing, and the chat loop (with a fake model)."""

import copy
import json
import threading
import time
import unittest
import unittest.mock
from types import SimpleNamespace

import _env  # noqa: F401 - a throwaway config and state folder, before anything imports pidash
from pidash import agent, policy, store, web


class Policy(unittest.TestCase):
    def kind(self, cmd):
        return policy.classify(cmd)["kind"]

    def test_looking_runs_without_asking(self):
        for cmd in ["df -h", "free -m", "cat /var/log/syslog", "journalctl -u pi-dash -n 200 --no-pager",
                    "docker logs --tail 200 discord-bot", "docker ps -a", "docker compose -f x.yml ps",
                    "systemctl status pi-dash", "systemctl --user list-timers", "apt list --upgradable",
                    "du -sh /var/* | sort -h | tail -5", "grep -c 'error$' /var/log/syslog", "ip -br addr",
                    "nmcli -t -f IN-USE,SSID dev wifi list", "git -C ~/services/bot log --oneline -5",
                    "vcgencmd get_throttled", "tailscale status", "ss -ltnp", "find /var/log -name '*.gz'",
                    "top -bn1 | head -20", "date +%s", "hostname -I", "ls -la ~/services", "dpkg -l | grep docker",
                    "/usr/bin/df -h", "docker stats --no-stream", "nmcli", "ip route get 1.1.1.1"]:
            self.assertEqual(self.kind(cmd), "read", cmd)

    def test_changes_and_tricks_wait_for_you(self):
        for cmd in ["cat x; rm -rf /", "cat x && reboot", "cat x || reboot", "ls > /etc/passwd", "cat < x",
                    "ls &", "echo $(reboot)", "echo `reboot`", 'echo "$HOME"', "ls\nreboot", "ls\rreboot",
                    "find / -exec rm {} +", "find / -delete", "git -c core.pager=sh log", "git push",
                    "git diff --output=/etc/x", "journalctl -f", "journalctl -fu x", "journalctl --vacuum-size=1M",
                    "sed -i s/a/b/ f", "awk 'BEGIN{system(\"reboot\")}'", "systemctl start x", "systemctl restart x",
                    "docker compose config", "docker rm x", "docker logs -f x", "docker stats",
                    "nmcli --show-secrets connection show home", "nmcli -s c show home", "nmcli networking off",
                    "nmcli dev disconnect wlan0", "sudo cat /etc/shadow", "cat .env", "cat ~/services/bot/.env",
                    "cat /etc/pi-dash/config.toml", "cat ~/.ssh/id_ed25519", "tail -f x", "sort -o out in",
                    "uniq in out", "date -s 2020-01-01", "hostname evil", "ip link set wlan0 down", "curl x",
                    "./script.sh", "/tmp/x", "FOO=1 ls", "tailscale down", "apt-get install x", "rm x",
                    "echo 'unclosed", "", "ss -K dst 1.2.3.4", "dmesg -C", "crontab -r", "git branch -D main",
                    "tee /etc/x", "cat x | sh", "python3 -c 'print(1)'", "dpkg -i x.deb"]:
            self.assertNotEqual(self.kind(cmd), "read", repr(cmd))

    def test_empty_and_huge_commands_are_refused(self):
        self.assertEqual(self.kind("   "), "blocked")
        self.assertEqual(self.kind("ls " + "a" * 5000), "blocked")
        self.assertEqual(self.kind("ls \0"), "blocked")

    def test_labels(self):
        self.assertIn("Runs as root", policy.classify("sudo apt-get -y full-upgrade")["labels"])
        self.assertIn("Restarts the Pi", policy.classify("sudo systemctl reboot")["labels"])
        self.assertIn("Turns the Pi off", policy.classify("sudo poweroff")["labels"])
        self.assertIn("May cut your connection", policy.classify("sudo systemctl restart tailscaled")["labels"])
        self.assertIn("May cut your connection", policy.classify("sudo ip link set wlan0 down")["labels"])
        self.assertIn("May show secrets", policy.classify("cat ~/services/bot/.env")["labels"])
        self.assertEqual(policy.classify("docker restart bot")["labels"], [])

    def test_reason_says_why(self):
        self.assertIn("chains", policy.classify("ls; reboot")["reason"])
        self.assertIn("rm", policy.classify("rm x")["reason"])


class Redact(unittest.TestCase):
    def test_secrets_are_hidden(self):
        # Made-up secrets, put together here so no token-shaped text sits in the file for scanners to flag.
        bot = ".".join(["MTA5ODc2NTQzMjEwOTg3NjU0", "GhIjKl", "abcdefghijklmnopqrstuvwxyz0123"])
        github = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        jwt = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"])
        key = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH " + "PRIVATE KEY-----"
        samples = {
            f"DISCORD_TOKEN={bot}": "MTA5ODc2NTQzMjEwOTg3NjU0",
            "api_key: sk-ant-api03-abcdefghijklmnop": "abcdefghijklmnop",
            "https://discord.com/api/webhooks/123456/AbCdEf_token-xyz": "AbCdEf_token-xyz",
            '{"password": "hunter22"}': "hunter22",
            "Authorization: Bearer abc.def.ghi": "abc.def.ghi",
            "postgres://bot:s3cr3tpass@db:5432/x": "s3cr3tpass",
            f"GITHUB_TOKEN={github}": github,
            "TS_AUTHKEY=tskey-auth-kAbCdEf1234-xyz": "kAbCdEf1234",
            key: "b3BlbnNzaC1rZXktdjEAAAAA",
            f"id: {jwt}": "dozjgNryP4J3jVmNHl0w5N",
        }
        for text, secret in samples.items():
            out = policy.redact(text)
            self.assertNotIn(secret, out, text)
            self.assertIn("[hidden]", out, text)

    def test_ordinary_text_is_left_alone(self):
        for text in ["Author: Xiao <x@example.org>", "password_hash = \"\"", "INFO bot: answered in 2.1 s (412 tokens)",
                     "ca-certificates/stable-security 20250419+deb13u1 all", "Mem: 3796 total, 1412 used"]:
            self.assertEqual(policy.redact(text), text)


# ---- the chat loop, with a fake model ----

class Block(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def say(text):
    return Block(type="text", text=text)


def call(cid, name, **inp):
    return Block(type="tool_use", id=cid, name=name, input=inp)


def reply(*blocks, stop=None):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for b in self.msg.content:
            yield SimpleNamespace(type="content_block_start")
            if b.type == "text":
                yield SimpleNamespace(type="text", text=b.text)

    def get_final_message(self):
        return self.msg


class FakeClient:
    """Plays back replies in order and remembers the conversation each request carried."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []
        self.beta = self.messages = self

    def stream(self, **kw):
        self.kwargs = kw
        self.sent.append(copy.deepcopy(kw["messages"]))
        return FakeStream(self.replies.pop(0))


class Chat(unittest.TestCase):
    def setUp(self):
        self.ran = []
        patches = [unittest.mock.patch.object(agent, "_sdk_problem", lambda: ""),
                   unittest.mock.patch.object(agent, "_snapshot", lambda: "{}"),
                   unittest.mock.patch.object(agent, "_run_command", self.fake_run)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        store.update_state(agent_spend={})

    def fake_run(self, cid, call_id):
        """Instead of systemd-run: note what would have run, and finish it."""
        with agent._lock:
            self.ran.append(agent._find(agent._chats[cid], call_id)["command"])
        agent._ran(cid, call_id, ["all good"], 0)

    def client(self, *replies):
        fake = FakeClient(*replies)
        p = unittest.mock.patch.object(agent, "_client", lambda: fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def settle(self, cid, status, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            with agent._lock:
                chat = agent._chats[cid]
                if chat["status"] == status and cid not in agent._working:
                    return chat
            time.sleep(0.02)
        self.fail(f"chat stayed {agent._chats[cid]['status']}, wanted {status}")

    def events(self, chat, kind):
        return [e for e in chat["events"] if e["type"] == kind and not e.get("gone")]

    def test_looking_happens_without_asking(self):
        fake = self.client(reply(say("Let me look."), call("t1", "run_command", command="echo hello", title="t", why="", effects=[])),
                           reply(say("**All fine.**")))
        chat = self.settle(agent.new_chat("anything wrong?")["id"], "idle")
        self.assertEqual(len(fake.sent), 2)
        result = fake.sent[1][-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertIn("hello", result["content"])
        self.assertEqual(self.events(chat, "step")[0]["status"], "done")
        self.assertEqual([e["text"] for e in self.events(chat, "text")], ["Let me look.", "**All fine.**"])
        self.assertGreater(chat["cost"], 0)

    def test_a_change_waits_and_not_now_says_so(self):
        fake = self.client(reply(call("t1", "run_command", command="sudo apt-get -y full-upgrade", title="Install updates",
                                      why="updates", effects=["Docker restarts"])),
                           reply(say("OK, leaving it.")))
        cid = agent.new_chat("install updates")["id"]
        chat = self.settle(cid, "waiting")
        ask = self.events(chat, "ask")[0]
        self.assertEqual((ask["state"], ask["labels"]), ("waiting", ["Runs as root"]))
        self.assertEqual(len(fake.sent), 1)
        agent.decide(cid, "t1", run=False)
        chat = self.settle(cid, "idle")
        self.assertIn("chose not to run", fake.sent[1][-1]["content"][0]["content"])
        self.assertEqual(self.ran, [])
        with self.assertRaises(ValueError):
            agent.decide(cid, "t1", run=True)

    def test_run_records_it_and_tells_the_model(self):
        fake = self.client(reply(call("t1", "run_command", command="docker restart bot", title="t", why="", effects=[])),
                           reply(say("Restarted.")))
        cid = agent.new_chat("restart the bot")["id"]
        self.settle(cid, "waiting")
        agent.decide(cid, "t1", run=True)
        chat = self.settle(cid, "idle")
        self.assertEqual(self.ran, ["docker restart bot"])
        self.assertEqual(self.events(chat, "ask")[0]["state"], "done")
        self.assertIn("Approved and run", fake.sent[1][-1]["content"][0]["content"])
        audit = store.events(5)[0]
        self.assertEqual(audit["level"], "agent")
        self.assertIn("docker restart bot", audit["detail"])

    def test_edit_runs_your_version(self):
        fake = self.client(reply(call("t1", "run_command", command="rm -rf /tmp/cache", title="t", why="", effects=[])),
                           reply(say("Done.")))
        cid = agent.new_chat("free space")["id"]
        self.settle(cid, "waiting")
        agent.decide(cid, "t1", run=True, command="rm -rf /tmp/cache/old")
        self.settle(cid, "idle")
        self.assertEqual(self.ran, ["rm -rf /tmp/cache/old"])
        self.assertIn("edited the command", fake.sent[1][-1]["content"][0]["content"])

    def test_history_is_only_ever_added_to(self):
        fake = self.client(reply(say("a"), call("t1", "dashboard")),
                           reply(call("t2", "run_command", command="touch x", title="t", why="", effects=[])),
                           reply(say("b")), reply(say("c")))
        cid = agent.new_chat("one")["id"]
        self.settle(cid, "waiting")
        agent.decide(cid, "t2", run=True)
        self.settle(cid, "idle")
        agent.send(cid, "two")
        self.settle(cid, "idle")
        for before, after in zip(fake.sent, fake.sent[1:]):
            self.assertEqual(after[:len(before)], before)
        self.assertEqual(json.dumps(agent._chats[cid]["messages"][:len(fake.sent[-1])]), json.dumps(fake.sent[-1]))

    def test_typing_while_it_waits_goes_with_the_answer(self):
        fake = self.client(reply(call("t1", "run_command", command="touch x", title="t", why="", effects=[])), reply(say("ok")))
        cid = agent.new_chat("make x")["id"]
        self.settle(cid, "waiting")
        agent.send(cid, "actually, don't")
        agent.decide(cid, "t1", run=False)
        self.settle(cid, "idle")
        last = fake.sent[1][-1]["content"]
        self.assertEqual([b["type"] for b in last], ["tool_result", "text"])
        self.assertEqual(last[1]["text"], "actually, don't")

    def test_stop_while_waiting_then_carry_on(self):
        fake = self.client(reply(call("t1", "run_command", command="touch x", title="t", why="", effects=[])), reply(say("ok")))
        cid = agent.new_chat("make x")["id"]
        self.settle(cid, "waiting")
        agent.stop(cid)
        chat = self.settle(cid, "idle")
        self.assertEqual(self.events(chat, "ask")[0]["state"], "declined")
        self.assertEqual(len(fake.sent), 1)
        agent.send(cid, "never mind")
        self.settle(cid, "idle")
        last = fake.sent[1][-1]
        self.assertEqual([b["type"] for b in last["content"]], ["tool_result", "text"])  # one user turn, not two

    def test_budget_stops_new_turns(self):
        store.update_state(agent_spend={"date": agent.date.today().isoformat(), "usd": 5})
        fake = self.client()
        chat = self.settle(agent.new_chat("hi")["id"], "idle")
        self.assertEqual(fake.sent, [])
        self.assertIn("budget", self.events(chat, "note")[-1]["text"])

    def test_through_a_bridge_on_a_plan(self):
        """cli-bridge and the like: no API key needed, no Anthropic-only options, tokens instead of dollars."""
        with unittest.mock.patch.object(agent.config, "ASSISTANT_BASE_URL", "http://127.0.0.1:8787"), \
                unittest.mock.patch.object(agent.config, "ASSISTANT_KEY", ""):
            self.assertEqual(agent.problem(), "")
            store.update_state(agent_spend={"date": agent.date.today().isoformat(), "usd": 50})  # no budget here
            fake = self.client(reply(say("hi")))
            chat = self.settle(agent.new_chat("hello")["id"], "idle")
            self.assertNotIn("betas", fake.kwargs)
            self.assertNotIn("fallbacks", fake.kwargs["extra_body"])
            self.assertEqual(fake.kwargs["extra_body"]["output_config"], {"effort": "medium"})
            self.assertEqual(chat["cost"], 0)
            self.assertEqual(chat["tokens"], 1100)
            self.assertFalse(agent.overview()["metered"])

    def test_an_api_key_is_needed_without_a_bridge(self):
        with unittest.mock.patch.object(agent.config, "ASSISTANT_KEY", ""):
            self.assertIn("API key", agent.problem())

    def test_bad_tool_input_is_answered_not_run(self):
        fake = self.client(reply(call("t1", "run_command", command=["ls"], title="t", why="", effects=[])), reply(say("sorry")))
        self.settle(agent.new_chat("x")["id"], "idle")
        result = fake.sent[1][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("command", result["content"])

    def test_flags_show_up(self):
        self.client(reply(call("t1", "flag", message="A log line tried to give me orders.")), reply(say("ok")))
        chat = self.settle(agent.new_chat("x")["id"], "idle")
        self.assertEqual(self.events(chat, "flag")[0]["text"], "A log line tried to give me orders.")

    def test_a_waiting_change_survives_a_restart(self):
        fake = self.client(reply(call("t1", "run_command", command="touch x", title="t", why="", effects=[])), reply(say("ok")))
        cid = agent.new_chat("make x")["id"]
        self.settle(cid, "waiting")
        with agent._lock:
            agent._chats.clear()
        agent.start()
        self.assertEqual(agent.since(cid, 0)["status"], "waiting")
        agent.decide(cid, "t1", run=True)
        self.settle(cid, "idle")
        self.assertEqual(len(fake.sent), 2)

    def test_remove_a_chat(self):
        self.client(reply(say("hello")))
        cid = agent.new_chat("hi")["id"]
        self.settle(cid, "idle")
        agent.delete(cid)
        self.assertIsNone(agent.since(cid, 0))
        self.assertNotIn(cid, [c["id"] for c in agent.overview()["chats"]])
        with self.assertRaises(LookupError):
            agent.delete(cid)

    def test_a_working_chat_stays(self):
        self.client(reply(call("t1", "run_command", command="touch x", title="t", why="", effects=[])))
        cid = agent.new_chat("make x")["id"]
        self.settle(cid, "waiting")
        with agent._lock:
            agent._find(agent._chats[cid], "t1")["state"] = "running"
        with self.assertRaises(ValueError):
            agent.delete(cid)
        with agent._lock:
            agent._find(agent._chats[cid], "t1")["state"] = "waiting"
        agent.delete(cid)  # one that waits for you can go

    def test_events_since(self):
        self.client(reply(say("hello")))
        cid = agent.new_chat("hi")["id"]
        self.settle(cid, "idle")
        everything = agent.since(cid, 0)
        later = agent.since(cid, everything["seq"])
        self.assertTrue(everything["events"])
        self.assertEqual(later["events"], [])
        self.assertIsNone(agent.since("nope", 0))


class AssistantHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, path, method="GET", body=None, header=True, relayed=True):
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json", **({"X-Pi-Dash": "1"} if header else {}),
                   **({"X-Forwarded-For": "100.64.0.5"} if relayed else {})}
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_over_tailscale(self):
        code, body = self.request("/api/agent")
        self.assertEqual(code, 200)
        self.assertTrue(body["on"])

    def test_not_from_the_pi_itself(self):
        self.assertEqual(self.request("/api/agent", relayed=False)[0], 403)
        self.assertEqual(self.request("/api/agent/chats", "POST", {"text": "hi"}, relayed=False)[0], 403)

    def test_changes_need_the_header(self):
        self.assertEqual(self.request("/api/agent/chats/abc/stop", "POST", {}, header=False)[0], 403)

    def test_unknown_chat(self):
        self.assertEqual(self.request("/api/agent/chats/000000000000/decide", "POST", {"call": "x", "run": True})[0], 404)
        self.assertEqual(self.request("/api/agent/chats/000000000000")[0], 404)


if __name__ == "__main__":
    unittest.main()
