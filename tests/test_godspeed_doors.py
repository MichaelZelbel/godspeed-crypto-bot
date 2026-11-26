"""Alerts, the key reminder and the page go through the owner's own Godspeed, and only through it.

Stand-ins: a local server that answers like Godspeed's deadline door (docs/full-version/addon-model.md) and
like Telegram's sendMessage, and fake `docker`/`systemctl`/`caddy` runners. Nothing here talks to a real one.
"""

import json
import os
import re
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from godspeed_crypto_bot import cli, godspeed, outbox, safety
from godspeed_crypto_bot.names import DISPLAY_NAME, NAME
from godspeed_crypto_bot.notify import Notifier

ROOT = Path(__file__).resolve().parent.parent


class Stand(BaseHTTPRequestHandler):
    """Godspeed's deadline door and Telegram's sendMessage in one local server."""

    deadlines: dict = {}
    sent: list = []
    touched: set = set()
    token = "123:abc"

    def log_message(self, *a):  # quiet
        pass

    def _answer(self, status, doc):
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._answer(200, {"version": "2.0.0-beta.9"}) if self.path == "/health" else self._answer(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path == f"/bot{self.token}/sendMessage":
            self.sent.append(body)
            return self._answer(200, {"ok": True, "result": {}})
        if self.path != "/api/functions/addon-deadlines":
            return self._answer(404, {})
        assert body["addon"] == NAME
        key, action = body.get("key"), body["action"]
        if action == "list":
            return self._answer(200, {"data": {"deadlines": list(self.deadlines.values())}})
        if action == "create":
            if key in self.deadlines:
                return self._answer(200, {"data": {"created": False, "deadline": self.deadlines[key]}})
            self.deadlines[key] = {"key": key, "title": body["title"], "due": body["due"], "state": "open",
                                   "touched": False, "done_when": body["done_when"], "cost": body["cost"]}
            return self._answer(200, {"data": {"created": True, "deadline": self.deadlines[key]}})
        if action == "update":
            if key in self.touched:
                return self._answer(200, {"data": {"updated": False, "touched": True}})
            self.deadlines[key]["due"] = body["due"]
            return self._answer(200, {"data": {"updated": True}})
        return self._answer(400, {"error": "unknown action"})


@pytest.fixture
def stand():
    Stand.deadlines, Stand.sent, Stand.touched = {}, [], set()
    server = HTTPServer(("127.0.0.1", 0), Stand)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", Stand
    server.shutdown()


@pytest.fixture
def gs(tmp_path):
    return godspeed.Godspeed(str(tmp_path))


@pytest.fixture
def hermes_env(tmp_path, cfg):
    env = tmp_path / "hermes" / ".env"
    env.parent.mkdir()
    env.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_ALLOWED_USERS=42\n", encoding="utf-8")
    cfg.telegram_env = str(env)
    return env


def relay_with(cfg, gs, stand_url, **kw):
    return godspeed.Relay(cfg, gs, door=godspeed.Door(gs, base=stand_url), **kw)


# ---- alerts


ALERTS = [  # every kind the bot sends: (key as the bot builds it, summary)
    ("halt:2026-11-26", "halted: the holdings differ from the books"),
    (f"drawdown:{date(2026, 11, 26)}", "stopped after a 25% loss and closed its positions"),
    ("guard-fix:unfixed", "the margin could not be fixed"),
    ("terms-changed:abc", "Hyperliquid changed its terms"),
    ("deadline:2026-11-26", "did not finish today's trading run by 00:45 UTC"),
    ("agent-expired:2026-11-26", "stopped: its Hyperliquid API wallet expired"),
    ("pair-failed:SOL", "a carry pair could not be completed and was reversed"),
]


def test_each_alert_kind_reaches_the_channel_once(cfg, store, gs, stand, hermes_env, monkeypatch):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    n = Notifier(cfg, store)
    for key, summary in ALERTS:
        n.alert(key, summary, "What happened.", "What to do.")
        n.alert(key, summary, "What happened.", "What to do.")  # the same alert in the same window: once
    out = relay_with(cfg, gs, url)()
    assert out["sent"] == len(ALERTS) and out["problems"] == [] and out["waiting"] == 0
    assert [m["chat_id"] for m in S.sent] == ["42"] * len(ALERTS)
    texts = [m["text"] for m in S.sent]
    for (_key, summary), text in zip(ALERTS, texts, strict=True):
        assert text.startswith(f"{DISPLAY_NAME} (practice, dry-run on mainnet): ") and summary in text
    assert relay_with(cfg, gs, url)()["sent"] == 0 and len(S.sent) == len(ALERTS)  # nothing sent twice


def test_a_live_alert_is_not_marked_as_practice(cfg, store, gs, stand, hermes_env, monkeypatch):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    cfg.mode = "live"
    Notifier(cfg, store).alert("k", "stopped", "Facts.", "Do this.")
    relay_with(cfg, gs, url)()
    assert S.sent[0]["text"] == f"{DISPLAY_NAME}: stopped. Facts. Do this."


def test_an_alert_waits_in_the_outbox_until_a_channel_takes_it(cfg, store, gs, stand, tmp_path, monkeypatch):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))  # no assistant here yet
    Notifier(cfg, store).alert("k", "stopped", "Facts.", "Do this.")
    out = relay_with(cfg, gs, url)()
    assert out["sent"] == 0 and out["waiting"] == 1 and "no Telegram connected" in out["problems"][0]
    assert out["channel"] is False and len(out["problems"]) == 1  # said once, not twice
    env = tmp_path / "hermes" / ".env"
    env.parent.mkdir()
    env.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_HOME_CHANNEL=7\n", encoding="utf-8")
    cfg.telegram_env = str(env)
    assert relay_with(cfg, gs, url)()["sent"] == 1 and S.sent[0]["chat_id"] == "7"


def test_a_failed_alert_or_write_never_stops_trading(cfg, store, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(outbox, "_write", broken)
    assert outbox.put_alert(cfg, "k", "text") is False  # logged, returns, nothing raised
    assert outbox.put_deadline(cfg, {"key": "k"}) is False
    assert Notifier(cfg, store).alert("k", "s", "status", "act") == -1


def test_a_status_only_run_sends_nothing(cfg, store, gs, stand, hermes_env, monkeypatch, tmp_path, capsys):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    conf = tmp_path / "c.toml"
    conf.write_text(f'state_dir = "{cfg.state_path.as_posix()}"\n')
    assert cli.main(["--config", str(conf), "status"]) == 0
    out = relay_with(cfg, gs, url)()
    assert out["sent"] == 0 and S.sent == [] and S.deadlines == {}


def test_the_relay_only_reads_small_regular_files(cfg, gs, stand, hermes_env, monkeypatch, tmp_path):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    box = outbox.directory(cfg)
    box.mkdir(parents=True)
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"text": "SECRET"}), encoding="utf-8")
    try:
        (box / "alert-00000000000000000001-aa.json").symlink_to(secret)
    except OSError:
        pytest.skip("no symlinks here")
    (box / "alert-00000000000000000002-bb.json").write_text("not json", encoding="utf-8")
    out = relay_with(cfg, gs, url)()
    assert out["sent"] == 0 and S.sent == []


# ---- the key reminder (the deadline door)


def _live_key(cfg, expires):
    cfg.mode, cfg.network = "live", "mainnet"
    return {"valid_until_ms": int(datetime(expires.year, expires.month, expires.day, tzinfo=timezone.utc).timestamp() * 1000)}


def test_the_deadline_door_gets_create_then_update(cfg, gs, stand):
    url, S = stand
    safety.request_key_reminder(cfg, _live_key(cfg, date(2027, 3, 1)))
    out = relay_with(cfg, gs, url)()
    d = S.deadlines[f"{godspeed.KEY_DEADLINE_KEY}-mainnet"]
    assert out["deadline"] == "created" and d["due"] == "2027-03-01"
    assert d["title"] == f"Renew {DISPLAY_NAME}'s exchange key before it expires"
    assert "preflight" in d["done_when"] and "keygen" in d["done_when"]
    assert relay_with(cfg, gs, url)()["deadline"] == "created"  # unchanged wish: Godspeed is not asked again
    safety.request_key_reminder(cfg, _live_key(cfg, date(2027, 8, 28)))  # the key was renewed
    out = relay_with(cfg, gs, url)()
    assert out["deadline"] == "date moved" and d["due"] == "2027-08-28" and len(S.deadlines) == 1


def test_a_reminder_the_owner_changed_is_left_alone(cfg, gs, stand):
    url, S = stand
    safety.request_key_reminder(cfg, _live_key(cfg, date(2027, 3, 1)))
    relay_with(cfg, gs, url)()
    S.touched.add(f"{godspeed.KEY_DEADLINE_KEY}-mainnet")
    safety.request_key_reminder(cfg, _live_key(cfg, date(2027, 8, 28)))
    assert relay_with(cfg, gs, url)()["deadline"].startswith("yours")
    assert next(iter(S.deadlines.values()))["due"] == "2027-03-01"


def test_no_reminder_without_a_live_mainnet_key(cfg, gs, stand):
    url, S = stand
    for mode, network in (("dry-run", "mainnet"), ("paper", "mainnet"), ("live", "testnet")):
        cfg.mode, cfg.network = mode, network
        safety.request_key_reminder(cfg, {"valid_until_ms": 1_800_000_000_000})
    relay_with(cfg, gs, url)()
    assert S.deadlines == {} and not (outbox.directory(cfg) / outbox.DEADLINE_FILE).exists()


def test_an_old_godspeed_without_the_door_is_reported_not_crashed(cfg, gs):
    class Old:
        def deadlines(self, body):
            raise RuntimeError("Processing function has not been ported: addon-deadlines")

    safety.request_key_reminder(cfg, _live_key(cfg, date(2027, 3, 1)))
    out = godspeed.Relay(cfg, gs, door=Old())()
    assert out["deadline"] is None and any("addon-deadlines" in p for p in out["problems"])


def test_the_docker_door_goes_through_the_godspeed_service(tmp_path):
    (tmp_path / ".godspeed").mkdir()
    (tmp_path / ".godspeed" / "model-host.json").write_text(json.dumps(
        {"format": 1, "kind": "godspeed-docker", "service": "godspeed", "workspace": "/opt/godspeed/workspace"}))
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    g = godspeed.find(str(tmp_path))
    assert g.kind == "docker"
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append((argv, stdin))
        return 0, json.dumps({"status": 200, "body": json.dumps({"data": {"deadlines": []}})}), ""

    assert godspeed.Door(g, run=run).deadlines({"action": "list"}) == {"deadlines": []}
    argv, stdin = calls[0]
    assert argv[:2] == ["docker", "compose"] and "exec" in argv and "--user" in argv and "godspeed" in argv
    assert json.loads(stdin)["path"] == "/api/functions/addon-deadlines"
    assert json.loads(json.loads(stdin)["body"]) == {"addon": NAME, "action": "list"}


def test_a_plain_folder_is_native(tmp_path):
    assert godspeed.find(str(tmp_path)).kind == "native"
    assert godspeed.find(str(tmp_path / "missing")) is None
    assert godspeed.find("relative/path") is None


@pytest.mark.parametrize("version,ok", [
    ("2.0.0-beta.6", True), ("2.0.0-beta.9", True), ("2.0.0-beta.5", False), ("2.0.0", True), ("2.1.0", True),
    ("2.0.0-rc.1", True), ("1.9.9", False), ("3.0.0-beta.1", True), (None, False), ("nonsense", False),
])
def test_the_minimum_godspeed(version, ok):
    assert godspeed.version_ok(version, "2.0.0-beta.6") is ok


def test_the_telegram_token_is_read_only_from_an_assistants_own_env(tmp_path):
    assert godspeed.valid_hermes_env("/root/.hermes/.env")
    assert godspeed.valid_hermes_env("/root/.hermes/profiles/work/.env")
    assert godspeed.valid_hermes_env("/opt/godspeed/.godspeed/integrated-runtime/hermes/.env")
    assert not godspeed.valid_hermes_env("/etc/passwd")
    assert not godspeed.valid_hermes_env("/home/x/.env")
    assert not godspeed.valid_hermes_env("/root/.hermes/../.env")


# ---- the page's address


def test_native_page_address_behind_godspeeds_sign_in(tmp_path):
    caddy = tmp_path / "etc/caddy"
    caddy.mkdir(parents=True)
    site = ("srv1.example.com:48443 {\n\timport /etc/caddy/godspeed-addons/paths/*.caddy\n"
            "\treverse_proxy 127.0.0.1:47833\n}\n\nimport /etc/caddy/godspeed-addons/*.caddy\n")
    (caddy / "godspeed-notebook.caddy").write_text(site)
    (caddy / "Caddyfile").write_text("import godspeed-notebook.caddy\n")
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        return 0, "", ""

    front = godspeed.native_front(str(tmp_path), run)
    assert front["kind"] == "caddy"
    address, why = godspeed.publish_native(front, str(tmp_path), run)
    assert (address, why) == (f"https://srv1.example.com:48443/{NAME}/", "")
    text = (caddy / "godspeed-addons/paths" / f"{NAME}.caddy").read_text()
    assert "forward_auth 127.0.0.1:47833" in text and "uri /api/session" in text and "redir * / 302" in text
    assert f"root * {godspeed.PAGE_DIR}" in text and f"handle_path /{NAME}/*" in text
    assert ["caddy", "validate", "--config", str(caddy / "Caddyfile"), "--adapter", "caddyfile"] in calls
    assert ["systemctl", "reload", "caddy"] in calls
    calls.clear()
    assert godspeed.publish_native(front, str(tmp_path), run)[0] == address and calls == []  # nothing changed


def test_a_caddy_that_refuses_the_file_gets_it_taken_out_again(tmp_path):
    caddy = tmp_path / "etc/caddy"
    caddy.mkdir(parents=True)
    (caddy / "godspeed-notebook.caddy").write_text(
        "srv1.example.com {\n\timport /etc/caddy/godspeed-addons/paths/*.caddy\n\treverse_proxy 127.0.0.1:47833\n}\n")
    front = {"kind": "caddy", "unit": "caddy.service", "caddyfile": "/etc/caddy/Caddyfile", "binary": "caddy",
             "text": (caddy / "godspeed-notebook.caddy").read_text()}
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        return 1, "", "invalid"

    address, why = godspeed.publish_native(front, str(tmp_path), run)
    assert address is None and "nothing was changed" in why
    assert not (caddy / "godspeed-addons/paths" / f"{NAME}.caddy").exists()
    assert calls == [["caddy", "validate", "--config", str(caddy / "Caddyfile"), "--adapter", "caddyfile"]]  # no reload


GODSPEED_CADDY = "/root/godspeed/.godspeed/integrated-runtime/runtime/caddy-2.11.4/caddy"
GODSPEED_CADDYFILE = "/root/godspeed/.godspeed/integrated-runtime/https/Caddyfile"
NOTEBOOK_SITE = ("srv1.example.com {\n\timport /etc/caddy/godspeed-addons/paths/*.caddy\n"
                 "\treverse_proxy 127.0.0.1:47833\n}\n\nimport /etc/caddy/godspeed-addons/*.caddy\n")


@pytest.mark.parametrize("line, argv", [
    # What Godspeed's own installer writes: the path in double quotes.
    (f'"{GODSPEED_CADDY}" run --config "{GODSPEED_CADDYFILE}" --adapter caddyfile',
     [GODSPEED_CADDY, "run", "--config", GODSPEED_CADDYFILE, "--adapter", "caddyfile"]),
    ("/usr/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile",
     ["/usr/bin/caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]),
    ('-"/opt/my caddy/caddy" run --config="/etc/my caddy/Caddyfile"',
     ["/opt/my caddy/caddy", "run", "--config=/etc/my caddy/Caddyfile"]),
    ("@/usr/bin/caddy caddy run --config /etc/caddy/Caddyfile",  # with @ the second word is only a shown name
     ["/usr/bin/caddy", "run", "--config", "/etc/caddy/Caddyfile"]),
    ("!!/usr/bin/caddy run --config /etc/caddy/Caddyfile", ["/usr/bin/caddy", "run", "--config", "/etc/caddy/Caddyfile"]),
    ("+:/usr/bin/caddy run", ["/usr/bin/caddy", "run"]),
    ('"/usr/bin/caddy run --config /etc/caddy/Caddyfile', []),  # an unbalanced quote: no program rather than a wrong one
    ("", []),
])
def test_the_exec_line_is_split_as_systemd_splits_it(line, argv):
    assert godspeed.exec_argv(line) == argv


def own_unit(tmp_path, exec_start, caddyfile):
    unit = tmp_path / "etc/systemd/system" / godspeed.OWN_UNIT
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"[Unit]\nDescription=Godspeed Mission Control web address\n[Service]\nUser=root\n"
                    f"ExecStart={exec_start}\nRestart=on-failure\n")
    site = tmp_path / caddyfile.lstrip("/")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(NOTEBOOK_SITE)


@pytest.mark.parametrize("quoted", [True, False])
def test_godspeeds_own_caddy_is_found_with_a_quoted_or_unquoted_path(tmp_path, quoted):
    q = '"' if quoted else ""
    own_unit(tmp_path, f"{q}{GODSPEED_CADDY}{q} run --config {q}{GODSPEED_CADDYFILE}{q} --adapter caddyfile",
             GODSPEED_CADDYFILE)
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        return 0, "", ""

    front = godspeed.native_front(str(tmp_path), run)
    assert front["kind"] == "own" and front["binary"] == GODSPEED_CADDY and front["caddyfile"] == GODSPEED_CADDYFILE
    address, why = godspeed.publish_native(front, str(tmp_path), run)
    assert (address, why) == (f"https://srv1.example.com/{NAME}/", "")
    assert calls == [[GODSPEED_CADDY, "validate", "--config", str(tmp_path / GODSPEED_CADDYFILE.lstrip("/")),
                      "--adapter", "caddyfile"],
                     ["systemctl", "restart", godspeed.OWN_UNIT]]


def test_a_failing_check_restarts_nothing_and_is_not_tried_again(tmp_path):
    """The 0.1.0 fault: the check could not even start (exit 127), and every relay restarted Godspeed's web address."""
    own_unit(tmp_path, f'"{GODSPEED_CADDY}" run --config "{GODSPEED_CADDYFILE}" --adapter caddyfile', GODSPEED_CADDYFILE)
    tried = tmp_path / "relay" / godspeed.PAGE_TRIED
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        return (127, "", "No such file or directory") if argv[1:2] == ["validate"] else (0, "", "")

    front = godspeed.native_front(str(tmp_path), run)
    address, why = godspeed.publish_native(front, str(tmp_path), run, tried)
    assert address is None and "nothing was changed" in why
    assert [c for c in calls if c[0] == "systemctl"] == [] and len(calls) == 1
    assert not (tmp_path / "etc/caddy/godspeed-addons/paths" / f"{NAME}.caddy").exists()
    calls.clear()
    for _ in range(5):  # the same attempt is never made again: no check, no restart
        address, why = godspeed.publish_native(front, str(tmp_path), run, tried)
        assert address is None and "install line runs again" in why
    assert calls == []
    tried.unlink()  # what the install line does: one more attempt
    godspeed.publish_native(front, str(tmp_path), run, tried)
    assert len(calls) == 1 and calls[0][1] == "validate"


def test_a_failed_restart_puts_back_once_and_never_loops(tmp_path):
    own_unit(tmp_path, f"{GODSPEED_CADDY} run --config {GODSPEED_CADDYFILE} --adapter caddyfile", GODSPEED_CADDYFILE)
    tried = tmp_path / "relay" / godspeed.PAGE_TRIED
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        return (1, "", "failed") if argv[:2] == ["systemctl", "restart"] else (0, "", "")

    front = godspeed.native_front(str(tmp_path), run)
    address, why = godspeed.publish_native(front, str(tmp_path), run, tried)
    assert address is None and "serves what it served before" in why
    assert [c for c in calls if c[0] == "systemctl"] == [["systemctl", "restart", godspeed.OWN_UNIT]] * 2
    calls.clear()
    assert godspeed.publish_native(front, str(tmp_path), run, tried)[0] is None and calls == []
    # A Godspeed update that changes its Caddy is a new attempt, and it works now.
    (tmp_path / GODSPEED_CADDYFILE.lstrip("/")).write_text(NOTEBOOK_SITE + "# updated\n")
    address, why = godspeed.publish_native(godspeed.native_front(str(tmp_path), lambda *a, **k: (0, "", "")),
                                           str(tmp_path), lambda *a, **k: (0, "", ""), tried)
    assert address == f"https://srv1.example.com/{NAME}/" and not tried.exists()


def test_the_relay_never_restarts_godspeeds_web_address_in_a_loop(cfg, tmp_path, monkeypatch):
    own_unit(tmp_path, f'"{GODSPEED_CADDY}" run --config "{GODSPEED_CADDYFILE}" --adapter caddyfile', GODSPEED_CADDYFILE)
    page_dir = tmp_path / "page"
    page_dir.mkdir()
    monkeypatch.setattr(godspeed, "PAGE_DIR", str(page_dir))
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        return (1, "", "invalid") if argv[1:2] == ["validate"] else (0, "", "")

    for _ in range(10):  # ten minutes of the relay's timer
        out = godspeed.Relay(cfg, godspeed.Godspeed(str(tmp_path)), run=run, root=str(tmp_path), tell=lambda t: True)()
        assert out["page"] is None and any("the page has no address" in p for p in out["problems"])
    assert [c for c in calls if c[1:2] == ["validate"]] == [calls[0]]
    assert [c for c in calls if c[:2] == ["systemctl", "restart"]] == []


def test_an_old_godspeed_without_add_on_paths_gets_a_plain_sentence(tmp_path):
    front = {"kind": "own", "unit": "godspeed-https.service", "caddyfile": "/x", "binary": "caddy",
             "text": "srv1.example.com {\n\treverse_proxy 127.0.0.1:47833\n}\n"}
    address, why = godspeed.publish_native(front, str(tmp_path), lambda *a, **k: (0, "", ""))
    assert address is None and "update Godspeed" in why


def test_the_page_is_never_published_without_the_sign_in_check():
    for site in (godspeed.path_site("47833"), godspeed.docker_site("srv1.example.com", 48445)):
        assert "forward_auth" in site and "uri /api/session" in site and "@signin status 401" in site


def test_docker_page_gets_its_own_caddy_container_on_godspeeds_network(tmp_path, monkeypatch):
    (tmp_path / ".godspeed").mkdir()
    (tmp_path / ".godspeed" / "model-host.json").write_text(json.dumps(
        {"format": 1, "kind": "godspeed-docker", "service": "godspeed", "workspace": "/w"}))
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("GODSPEED_HOST=srv1.example.com\n")
    page_dir = tmp_path / "page"
    page_dir.mkdir()
    monkeypatch.setattr(godspeed, "PAGE_DIR", str(page_dir))
    g = godspeed.find(str(tmp_path))
    calls = []
    inspected = json.dumps([{"State": {"Running": True}, "Config": {"Image": "caddy:2.10.2-alpine"},
                             "Mounts": [{"Destination": "/data", "Type": "volume", "Name": "caddy_data"}],
                             "NetworkSettings": {"Networks": {"godspeed_default": {}}}}])

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        if argv[:2] == ["docker", "compose"] and "ps" in argv:
            return 0, "abcdef123456\n", ""
        if argv[:2] == ["docker", "inspect"] and argv[2] == "abcdef123456":
            return 0, inspected, ""
        if argv[:2] == ["docker", "inspect"]:
            return 1, "", "no such container"
        return 0, "", ""

    address, why = godspeed.publish_docker(g, run, config_dir=str(tmp_path / "front"))
    assert address == "https://srv1.example.com:48445/" and why == ""
    runs = [c for c in calls if c[:3] == ["docker", "run", "-d"]]
    assert len(runs) == 1
    flat = " ".join(runs[0])
    assert "--network godspeed_default" in flat and "-p 48445:48445" in flat and "type=volume,src=caddy_data,dst=/data" in flat
    assert f"src={page_dir},dst=/srv/page,readonly" in flat and "caddy:2.10.2-alpine" in flat
    assert "forward_auth godspeed:47831" in (tmp_path / "front" / "Caddyfile").read_text()


def test_the_relay_records_the_address_for_status(cfg, tmp_path, monkeypatch):
    page_dir = tmp_path / "page"
    page_dir.mkdir()
    monkeypatch.setattr(godspeed, "PAGE_DIR", str(page_dir))
    monkeypatch.setattr(godspeed, "publish_page", lambda *a, **k: ("https://srv1.example.com/x/", ""))
    out = godspeed.Relay(cfg, godspeed.Godspeed(str(tmp_path)), tell=lambda t: True)()
    assert out["page"] == "https://srv1.example.com/x/"
    from godspeed_crypto_bot import bot, records

    s = records.Store(cfg.db_path, cfg.mode, cfg.network)
    st = bot.status(cfg, s)
    s.close()
    assert st["page"] == "https://srv1.example.com/x/" and st["godspeed"]["problems"] == []


# ---- nothing of the old private machinery is left


def test_nothing_of_the_private_machinery_is_left():
    # character classes, so that this test does not contain what it looks for
    parts = ["mc-notif[y]", "godspeed jo[b]", "mc-artifact[s]", "expires[.]txt", "srv[0-9]{7}",
             "godspeed-mission-control-crypt[o]"]
    pattern = re.compile("|".join(parts))
    for folder in ("godspeed_crypto_bot", "deploy", "tests"):
        for p in (ROOT / folder).rglob("*"):
            if p.is_file() and p.suffix in (".py", ".sh", ".toml", ".service", ".timer", ".md", ""):
                if p.name in ("requirements.txt", "test_private_details.py", Path(__file__).name) or "__pycache__" in p.parts:
                    continue
                assert not pattern.search(p.read_text(encoding="utf-8", errors="ignore")), p


# ---- what the review of the first version found


def test_no_alert_is_ever_thrown_away_however_long_the_channel_was_down(cfg, store, gs, stand, hermes_env, monkeypatch):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    n = Notifier(cfg, store)
    total = godspeed.SENDS_PER_RELAY * 2 + 7
    for i in range(total):
        n.alert(f"k{i}", f"problem {i}", "Facts.", "Do this.")
    assert len(outbox.waiting_alerts(cfg)) == total  # nothing dropped while waiting
    while outbox.waiting_alerts(cfg):
        relay_with(cfg, gs, url)()
    assert [m["text"].split(": ")[1].split(".")[0] for m in S.sent] == [f"problem {i}" for i in range(total)]


def test_a_reminder_lost_in_godspeed_comes_back(cfg, gs, stand, monkeypatch):
    url, S = stand
    safety.request_key_reminder(cfg, _live_key(cfg, date(2027, 3, 1)))
    relay_with(cfg, gs, url)()
    S.deadlines.clear()  # deleted in Godspeed, or lost in a recovery
    assert relay_with(cfg, gs, url)()["deadline"] == "created" and S.deadlines == {}  # not asked again within hours
    real = godspeed.time.time
    monkeypatch.setattr(godspeed.time, "time", lambda: real() + godspeed.DEADLINE_RECHECK_S + 60)
    out = relay_with(cfg, gs, url)()
    assert out["deadline"] == "created" and len(S.deadlines) == 1


def test_the_relays_own_files_are_outside_the_directory_the_bot_can_write(cfg, gs, stand, hermes_env, monkeypatch):
    url, _S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    safety.request_key_reminder(cfg, _live_key(cfg, date(2027, 3, 1)))
    relay_with(cfg, gs, url)()
    assert cfg.relay_path != cfg.state_path and cfg.state_path not in cfg.relay_path.parents
    assert {p.name for p in cfg.relay_path.iterdir()} >= {"relay-status.json", "deadline-synced.json"}
    assert not any(p.name.startswith("relay-status") or "synced" in p.name for p in cfg.state_path.rglob("*"))


def test_a_linked_outbox_is_not_followed(cfg, gs, stand, tmp_path, hermes_env, monkeypatch):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "alert-00000000000000000001-aa.json").write_text(json.dumps({"text": "SECRET"}))
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    try:
        outbox.directory(cfg).symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    assert relay_with(cfg, gs, url)()["sent"] == 0 and S.sent == []
    assert (elsewhere / "alert-00000000000000000001-aa.json").exists()


def test_a_renewal_keeps_the_old_key_aside_and_makes_a_new_one(cfg):
    from godspeed_crypto_bot import keys
    from godspeed_crypto_bot.config import CREDENTIAL_PREFIX
    from tests.test_keys_terms_export import FakeCreds

    fc = FakeCreds()
    first = keys.keygen(cfg, "mainnet", encrypt=fc.encrypt, decrypt=fc.decrypt)
    with pytest.raises(FileExistsError, match="--replace"):
        keys.keygen(cfg, "mainnet", encrypt=fc.encrypt, decrypt=fc.decrypt)
    second = keys.keygen(cfg, "mainnet", encrypt=fc.encrypt, decrypt=fc.decrypt, replace=True)
    assert second != first and keys.address_path(cfg, "mainnet").read_text().strip() == second
    kept = [p for p in (keys.address_path(cfg, "mainnet").parent / "replaced").iterdir() if "agent-address" in p.name]
    assert len(kept) == 1 and kept[0].read_text().strip() == first
    # nothing but the working key sits in the credential store, where a unit imports everything by name
    assert [p.name for p in keys.Path(cfg.credstore_dir).iterdir()] == [f"{CREDENTIAL_PREFIX}.mainnet"]
    # and a failed renewal leaves the working key where it was
    working = keys.Path(cfg.credstore_dir) / f"{CREDENTIAL_PREFIX}.mainnet"
    before = working.read_bytes()
    with pytest.raises(RuntimeError):
        keys.keygen(cfg, "mainnet", encrypt=fc.encrypt, decrypt=lambda n, p: b"other", replace=True)
    assert working.read_bytes() == before and keys.address_path(cfg, "mainnet").read_text().strip() == second


NL = chr(10)


def test_the_relay_command_also_serves_a_second_track(cfg, tmp_path, monkeypatch, capsys):
    from godspeed_crypto_bot import page

    calls = []
    monkeypatch.setattr(godspeed.Relay, "__call__", lambda self: calls.append(self.cfg.state_dir) or {})
    conf = tmp_path / "c.toml"
    conf.write_text(NL.join([f'godspeed_dir = "{tmp_path.as_posix()}"',
                               f'state_dir = "{(tmp_path / "main").as_posix()}"']))
    second = tmp_path / "config.testnet.toml"
    second.write_text(NL.join([f'state_dir = "{(tmp_path / "testnet").as_posix()}"',
                                 f'relay_dir = "{(tmp_path / "relay-t").as_posix()}"']))
    monkeypatch.setattr(page, "TESTNET_CONFIG", second)
    assert cli.main(["--config", str(conf), "relay"]) == 0
    assert calls == [(tmp_path / "main").as_posix(), (tmp_path / "testnet").as_posix()]


def test_a_renewed_key_does_not_inherit_the_old_keys_expiry(cfg, store):
    """The new API wallet has another address; if the venue gives no validUntil for it, the old date must not stay."""
    from datetime import timedelta

    from .test_bot import TODAY, at

    class Venue:
        signs = True

        def __init__(self, address, valid_until=None):
            self.address, self.valid_until = address, valid_until

        def agent_validity(self):
            ms = int(self.valid_until.timestamp() * 1000) if self.valid_until else None
            return {"address": self.address, "listed": True, "valid_until_ms": ms}

    now = at(TODAY, 0, 45)
    old_expiry = at(TODAY + timedelta(days=3), 12)
    safety.agent_expiry(cfg, store, Venue("0xold", old_expiry), now)
    assert store.get("agent")["valid_until_ms"] == int(old_expiry.timestamp() * 1000)
    cfg.agent_approved_on = TODAY.isoformat()  # the renewal: approved today, so 180 days
    rec = safety.agent_expiry(cfg, store, Venue("0xnew"), now)
    assert rec["address"] == "0xnew" and rec["source"].startswith("config agent_approved_on")
    assert datetime.fromtimestamp(rec["valid_until_ms"] / 1000, tz=timezone.utc).date() == TODAY + timedelta(days=180)
    cfg.agent_approved_on = ""
    rec = safety.agent_expiry(cfg, store, Venue("0xthird"), now)
    assert "valid_until_ms" not in rec  # unknown rather than wrong


def test_the_names_come_from_one_place():
    """Every command name and display name a person meets is spelled in names.py only (strings and prompts)."""
    import ast

    for p in (ROOT / "godspeed_crypto_bot").glob("*.py"):
        if p.name == "names.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert NAME not in node.value and DISPLAY_NAME not in node.value, (p.name, node.lineno, node.value[:60])


# ---- a channel for the messages, before one is needed


def test_a_missing_channel_is_said_before_a_message_needs_it(cfg, gs, stand, tmp_path, monkeypatch):
    url, _S = stand
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))  # a Godspeed with no Telegram connected
    out = relay_with(cfg, gs, url)()
    assert out["waiting"] == 0 and out["channel"] is False
    assert out["problems"] == [f"messages cannot reach you: {godspeed.NO_CHANNEL}"]
    assert "set telegram_env" not in out["problems"][0] and "Godspeed has no Telegram connected" in out["problems"][0]


def test_the_channel_is_looked_at_every_ten_minutes_and_found_once_connected(cfg, gs, stand, tmp_path, monkeypatch):
    url, _S = stand
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    calls = []
    real = godspeed.find_telegram
    monkeypatch.setattr(godspeed, "find_telegram", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert relay_with(cfg, gs, url)()["channel"] is False
    assert relay_with(cfg, gs, url)()["channel"] is False and len(calls) == 1  # remembered, not asked every minute
    env = tmp_path / "hermes" / ".env"
    env.parent.mkdir()
    env.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_HOME_CHANNEL=7\n", encoding="utf-8")
    cfg.telegram_env = str(env)
    seen = cfg.relay_path / godspeed.CHANNEL_FILE
    old = seen.stat().st_mtime - godspeed.CHANNEL_RECHECK_S - 1
    os.utime(seen, (old, old))
    out = relay_with(cfg, gs, url)()
    assert out["channel"] is True and out["problems"] == [] and len(calls) == 2


def test_test_alert_sends_one_test_message_through_the_channel(cfg, store, gs, stand, hermes_env, monkeypatch,
                                                              tmp_path, capsys):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    conf = tmp_path / "c.toml"
    conf.write_text(f'state_dir = "{cfg.state_path.as_posix()}"\nmode = "live"\nnetwork = "mainnet"\n')
    assert cli.main(["--config", str(conf), "test-alert"]) == 0
    said = capsys.readouterr().out
    assert "queued" in said and "Check whether it arrives" in said
    for promise in ("within", "reaches you", "on its way", "minute", "will arrive", "is sent"):
        assert promise not in said
    assert cli.main(["--config", str(conf), "test-alert"]) == 0  # asked again, sent again
    out = relay_with(cfg, gs, url)()
    assert out["sent"] == 2 and len(S.sent) == 2
    for m in S.sent:  # a test in every mode, live on mainnet too
        assert m["text"].startswith(f"{DISPLAY_NAME} (test message): ") and "Nothing happened" in m["text"]
    from godspeed_crypto_bot import records

    s = records.Store(cfg.db_path, "live", "mainnet")
    assert s.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0  # nothing happened: no alert recorded
    s.close()


def test_what_a_delivery_sees_is_remembered_at_once(cfg, store, gs, stand, tmp_path, monkeypatch):
    url, S = stand
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TEST", "1")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_TELEGRAM_API", url)
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    assert relay_with(cfg, gs, url)()["channel"] is False  # remembered for ten minutes
    env = tmp_path / "hermes" / ".env"
    env.parent.mkdir()
    env.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_HOME_CHANNEL=7\n", encoding="utf-8")
    cfg.telegram_env = str(env)
    Notifier(cfg, store).alert("k1", "stopped", "Facts.", "Do this.")
    out = relay_with(cfg, gs, url)()  # inside the ten minutes, but a message went out: there is a channel
    assert out["sent"] == 1 and out["channel"] is True and out["problems"] == []
    assert relay_with(cfg, gs, url)()["channel"] is True
    env.unlink()  # Telegram disconnected again, still inside the ten minutes
    Notifier(cfg, store).alert("k2", "stopped again", "Facts.", "Do this.")
    out = relay_with(cfg, gs, url)()
    assert out["sent"] == 0 and out["channel"] is False and len(out["problems"]) == 1
