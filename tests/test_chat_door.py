"""The chat door: requests the assistant writes in its mission control folder, answered by the root relay.

Each request kind end to end against a temporary folder; every refusal answered with one sentence that changes nothing;
use-on-paper refused while live; results beside the requests; a row in the records for each; the folder and the recipe
set up on a native and a Docker Godspeed; and no way through a link out of the assistant's folder."""

import json
import os
import re
from pathlib import Path

import pytest

from godspeed_crypto_bot import backtest, chat_door, config, godspeed, records, strategy_file

ROOT = Path(__file__).resolve().parent.parent
PERPS = {"BTC", "ETH", "SOL", "DOGE"}
MOMO = """name = "momo"
coins = ["BTC", "ETH"]
rebalance = "weekly"
direction = "long-short"

[signal]
kind = "momentum"
lookbacks = [20, 60]

[sizing]
method = "equal"
gross_exposure = 1.0
"""
REPORT = backtest.FIRST_LINE + "\n\n# Crypto Bot backtest: momo\n\n- Worst day: 2025-03-02, -11.5%.\n"
POSIX = chat_door.POSIX_FDS


class Bed:
    """A configuration, the bot's own strategies folder, and the assistant's mission control folder."""

    def __init__(self, tmp_path, mode="paper"):
        self.tmp = tmp_path
        self.config_path = tmp_path / "etc" / "config.toml"
        self.config_path.parent.mkdir()
        self.config_path.write_text(f'mode = "{mode}"\nstrategies_dir = "{(tmp_path / "etc" / "strategies").as_posix()}"\n'
                                    f'state_dir = "{(tmp_path / "state").as_posix()}"\n'
                                    f'relay_dir = "{(tmp_path / "relay").as_posix()}"\n', encoding="utf-8")
        self.mc = tmp_path / "mc"
        self.mc.mkdir()
        chat_door.setup((self.mc, []), ROOT, "test")
        self.backtests = []

    @property
    def cfg(self):
        return config.load(self.config_path)

    def strategy(self, name="momo", text=MOMO):
        (self.mc / "crypto-bot" / "strategies" / f"{name}.toml").write_text(text, encoding="utf-8")

    def request(self, rid, doc):
        body = doc if isinstance(doc, (str, bytes)) else json.dumps(doc)
        p = self.mc / "crypto-bot" / "requests" / f"{rid}.json"
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
        return p

    def fake_backtest(self, cfg, name, start, end, out_name):
        self.backtests.append((name, start, end, out_name))
        return REPORT

    def door(self, perps=lambda: set(PERPS)):
        return chat_door.Door(self.cfg, self.config_path, (self.mc, []), perps, backtest=self.fake_backtest)

    def answer(self, rid):
        req = self.mc / "crypto-bot" / "requests"
        return (req / f"{rid}.result.md").read_text(encoding="utf-8"), json.loads(
            (req / f"{rid}.result.json").read_text(encoding="utf-8"))


@pytest.fixture
def bed(tmp_path):
    return Bed(tmp_path)


# ---- each kind, end to end


def test_check_answers_what_the_bot_understood_and_takes_the_file(bed):
    bed.strategy()
    bed.request("2026-11-27-check", {"request": "check", "strategy": "momo"})
    out = bed.door()()
    assert out == {"answered": ["2026-11-27-check.json"]}
    text, doc = bed.answer("2026-11-27-check")
    spec = strategy_file.check_bytes(MOMO.encode(), perps=PERPS)
    assert text.startswith(f"Checked: the strategy file momo.toml can be used (fingerprint {spec.fingerprint}).")
    assert strategy_file.explain(spec) in text
    assert doc["outcome"] == "done" and doc["kind"] == "check" and doc["fingerprint"] == spec.fingerprint
    assert (bed.tmp / "etc" / "strategies" / "momo.toml").read_text(encoding="utf-8") == MOMO
    assert bed.cfg.strategy == "mix"  # a check changes nothing the bot runs


def test_backtest_answers_with_the_report(bed):
    bed.strategy()
    bed.request("bt1", {"request": "backtest", "strategy": "momo", "start": "2025-01-01", "end": "2025-04-30"})
    bed.door()()
    text, doc = bed.answer("bt1")
    assert text.startswith("Tested: the strategy file momo.toml (fingerprint ") and REPORT in text
    assert doc["outcome"] == "done" and doc["kind"] == "backtest"
    from datetime import date

    assert bed.backtests == [("momo", date(2025, 1, 1), date(2025, 4, 30), "chat-bt1")]


def test_use_on_paper_switches_the_strategy_in_paper_mode(bed):
    bed.strategy()
    bed.request("use", {"request": "use-on-paper", "strategy": "momo"})
    bed.door()()
    text, doc = bed.answer("use")
    assert text.startswith('Done: from the next daily run (00:02 UTC) Crypto Bot runs your own strategy "momo"')
    assert doc["outcome"] == "done" and bed.cfg.strategy == "file:momo"
    assert strategy_file.load_for(bed.cfg).name == "momo"  # the bot reads it from its own folder
    assert "on hold" not in text


def test_use_on_paper_says_so_when_trading_is_on_hold(bed):
    """Seen on the fresh-server walk-through: the answer said the strategy runs from the next daily run while a HALT
    file kept the bot from trading at all."""
    bed.strategy()
    bed.cfg.halt_path.parent.mkdir(parents=True, exist_ok=True)
    bed.cfg.halt_path.write_text("2026-11-27T00:00:00+00:00 by hand\n", encoding="utf-8")
    bed.request("use", {"request": "use-on-paper", "strategy": "momo"})
    bed.door()()
    text, doc = bed.answer("use")
    assert doc["outcome"] == "done" and bed.cfg.strategy == "file:momo"  # still switched, as asked
    assert "Trading is on hold now (a HALT file is there)" in text and text.rstrip().endswith("godspeed-crypto-bot resume")
    assert doc["said"].endswith("Trading is on hold until resume.")


def test_every_request_and_its_outcome_is_a_row_in_the_records(bed):
    bed.strategy()
    bed.request("a", {"request": "check", "strategy": "momo"})
    bed.request("b", {"request": "check", "strategy": "nothere"})
    bed.door()()
    cfg = bed.cfg
    store = records.Store(cfg.db_path, cfg.mode, cfg.network)
    try:
        assert chat_door.import_log(cfg, store) == 2
        assert chat_door.import_log(cfg, store) == 0  # once each
        rows = {r["request"]: dict(r) for r in store.chat_requests()}
        assert rows["a"]["outcome"] == "done" and rows["a"]["strategy"] == "momo" and rows["a"]["kind"] == "check"
        assert rows["b"]["outcome"] == "refused" and "There is no strategy file nothere.toml" in rows["b"]["said"]
    finally:
        store.close()


def test_a_request_is_answered_once_and_the_answer_sits_beside_it(bed):
    bed.strategy()
    bed.request("once", {"request": "check", "strategy": "momo"})
    assert bed.door()()["answered"] == ["once.json"]
    assert bed.door()()["answered"] == []
    names = sorted(os.listdir(bed.mc / "crypto-bot" / "requests"))
    assert names == ["once.json", "once.result.json", "once.result.md"]


def test_a_backtest_ends_the_runs_share_and_at_most_five_are_answered_a_run(bed):
    bed.strategy()
    for k in range(7):
        bed.request(f"c{k}", {"request": "check", "strategy": "momo"})
    assert len(bed.door()()["answered"]) == chat_door.PER_RUN
    assert len(bed.door()()["answered"]) == 2
    bed.request("t1", {"request": "backtest", "strategy": "momo"})
    bed.request("t2", {"request": "backtest", "strategy": "momo"})
    assert len(bed.door()()["answered"]) == 1  # one backtest a run: the relay's other work goes on


# ---- refusals: one sentence, nothing changed


REFUSALS = [
    (b"not json", "This request is not JSON"),
    (b"[1, 2]", "This request is not a JSON object"),
    ({"request": "check", "strategy": "momo", "run": "rm -rf /"}, "settings the bot does not know (run)"),
    ({"request": "go-live", "strategy": "momo"}, 'The request must be "check", "backtest" or "use-on-paper".'),
    ({"request": "check", "strategy": "../etc/passwd"}, "The request must name a strategy file"),
    ({"request": "check"}, "The request must name a strategy file"),
    ({"request": "check", "strategy": "momo", "start": "2025-01-01"}, "Only a backtest takes a start day."),
    ({"request": "backtest", "strategy": "momo", "end": "May 2025"}, "The end day must be written YYYY-MM-DD."),
    ({"request": "check", "strategy": "missing"}, "There is no strategy file missing.toml in crypto-bot/strategies."),
]


@pytest.mark.parametrize("doc,sentence", REFUSALS, ids=[s[:40] for _d, s in REFUSALS])
def test_a_malformed_request_is_answered_with_one_sentence_and_changes_nothing(bed, doc, sentence):
    bed.strategy()
    before = bed.config_path.read_text()
    bed.request("bad", doc)
    bed.door()()
    text, result = bed.answer("bad")
    assert text.startswith("Refused: ") and sentence in text and text.count("\n") == 1
    assert result["outcome"] == "refused"
    assert bed.config_path.read_text() == before
    assert not (bed.tmp / "etc" / "strategies").exists()


def test_a_failing_strategy_is_refused_with_every_problem_and_never_reaches_the_bot(bed):
    bed.strategy(text=MOMO.replace('"weekly"', '"hourly"').replace('"ETH"]', '"XYZ"]'))
    bed.request("f", {"request": "use-on-paper", "strategy": "momo"})
    bed.door()()
    text, result = bed.answer("f")
    assert text.startswith("Refused, for these reasons:\n")
    assert "- XYZ is not a perpetual on Hyperliquid.\n" in text and '- rebalance must be "daily"' in text
    assert text.endswith("Nothing was changed.\n") and result["outcome"] == "refused"
    assert not (bed.tmp / "etc" / "strategies" / "momo.toml").exists() and bed.cfg.strategy == "mix"


def test_use_on_paper_is_refused_while_live_with_what_to_type(tmp_path):
    bed = Bed(tmp_path, mode="live")
    bed.strategy()
    before = bed.config_path.read_text()
    bed.request("live", {"request": "use-on-paper", "strategy": "momo"})
    bed.door()()
    text, result = bed.answer("live")
    assert text.startswith("Refused: Crypto Bot is trading real money, so its strategy is not changed from the chat.")
    assert "godspeed-crypto-bot configure --strategy file:momo" in text and "godspeed-crypto-bot go-live" in text
    assert result["outcome"] == "refused" and bed.config_path.read_text() == before
    assert not (bed.tmp / "etc" / "strategies" / "momo.toml").exists()


def test_a_changed_version_of_the_running_strategy_is_not_slipped_in_by_a_check(bed):
    bed.strategy()
    bed.request("use", {"request": "use-on-paper", "strategy": "momo"})
    bed.door()()
    running = (bed.tmp / "etc" / "strategies" / "momo.toml").read_bytes()
    bed.strategy(text=MOMO.replace("[20, 60]", "[10, 30]"))
    bed.request("chk", {"request": "backtest", "strategy": "momo"})
    bed.door()()
    text, _ = bed.answer("chk")
    assert 'Crypto Bot runs "momo" now, and this file is a changed version of it.' in text
    assert (bed.tmp / "etc" / "strategies" / "momo.toml").read_bytes() == running and bed.backtests == []
    bed.request("use2", {"request": "use-on-paper", "strategy": "momo"})  # with the person's yes it is taken
    bed.door()()
    assert b"[10, 30]" in (bed.tmp / "etc" / "strategies" / "momo.toml").read_bytes()


def test_without_hyperliquid_nothing_is_accepted(bed):
    bed.strategy()
    bed.request("off", {"request": "check", "strategy": "momo"})

    def offline():
        raise ConnectionError("no network")

    bed.door(perps=offline)()
    text, _ = bed.answer("off")
    assert "Hyperliquid could not be reached to check that the coins exist" in text


def test_files_that_are_not_requests_are_left_alone(bed):
    req = bed.mc / "crypto-bot" / "requests"
    (req / "notes.txt").write_text("hello")
    (req / "has space.json").write_text("{}")
    (req / ".hidden.json").write_text("{}")
    assert bed.door()()["answered"] == []
    assert sorted(os.listdir(req)) == [".hidden.json", "has space.json", "notes.txt"]


# ---- no way out of the assistant's folder through a link


@pytest.mark.skipif(not POSIX, reason="links and directory descriptors are POSIX")
def test_a_linked_requests_folder_is_never_followed(bed, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.json").write_text(json.dumps({"request": "check", "strategy": "momo"}))
    req = bed.mc / "crypto-bot" / "requests"
    os.rmdir(req)
    os.symlink(elsewhere, req)
    out = bed.door()()
    assert out["answered"] == [] and "not a plain folder" in out["problem"]
    assert sorted(os.listdir(elsewhere)) == ["x.json"]  # nothing written there


@pytest.mark.skipif(not POSIX, reason="links are POSIX")
def test_a_linked_request_or_strategy_file_is_never_read(bed, tmp_path):
    secret = tmp_path / "secret.toml"
    secret.write_text(MOMO)
    os.symlink(secret, bed.mc / "crypto-bot" / "strategies" / "momo.toml")
    bed.request("l1", {"request": "check", "strategy": "momo"})
    os.symlink(tmp_path / "secret.toml", bed.mc / "crypto-bot" / "requests" / "l2.json")
    bed.door()()
    t1, _ = bed.answer("l1")
    t2, _ = bed.answer("l2")
    assert t1.startswith("Refused: momo.toml cannot be read") and t2.startswith("Refused: l2.json cannot be read")


@pytest.mark.skipif(not POSIX, reason="links are POSIX")
def test_an_answer_replaces_a_planted_link_instead_of_writing_through_it(bed, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep")
    bed.strategy()
    bed.request("p", {"request": "check", "strategy": "momo"})
    os.symlink(target, bed.mc / "crypto-bot" / "requests" / "p.result.md")
    bed.door()()
    assert target.read_text() == "keep"
    assert not os.path.islink(bed.mc / "crypto-bot" / "requests" / "p.result.md")


# ---- the folder and the recipe


def test_setup_makes_the_folder_keeps_requests_out_of_git_and_puts_the_recipe_with_its_reference(tmp_path):
    mc = tmp_path / "mc"
    (mc / "skills" / "notes").mkdir(parents=True)
    (mc / "skills" / "notes" / "SKILL.md").write_text("x")
    said = chat_door.setup((mc, []), ROOT, "v0.2.0")
    folder = mc / "crypto-bot"
    assert (folder / "strategies").is_dir() and (folder / "requests").is_dir()
    assert "requests/" in (folder / ".gitignore").read_text().splitlines()
    recipe = mc / "skills" / "crypto-bot"
    assert (recipe / "SKILL.md").read_text(encoding="utf-8") == (ROOT / "skill" / "crypto-bot" / "SKILL.md").read_text(
        encoding="utf-8")
    assert (recipe / "strategy-files.md").read_text(encoding="utf-8") == (ROOT / "docs" / "strategy-files.md").read_text(
        encoding="utf-8")
    assert (recipe / chat_door.RECIPE_MARKER).read_text() == "godspeed-crypto-bot v0.2.0\n"
    assert any("crypto-bot" in s for s in said)
    chat_door.setup((mc, []), ROOT, "v0.2.1")  # running the line again replaces its own recipe
    assert (recipe / chat_door.RECIPE_MARKER).read_text() == "godspeed-crypto-bot v0.2.1\n"


def test_setup_leaves_a_recipe_the_person_wrote_alone_and_uses_claude_skills_when_only_that_has_recipes(tmp_path):
    mc = tmp_path / "mc"
    (mc / "skills" / "crypto-bot").mkdir(parents=True)
    (mc / "skills" / "crypto-bot" / "SKILL.md").write_text("mine")
    said = chat_door.setup((mc, []), ROOT, "v")
    assert (mc / "skills" / "crypto-bot" / "SKILL.md").read_text() == "mine"
    assert any(s.startswith("Left alone:") for s in said)
    other = tmp_path / "other"
    (other / ".claude" / "skills" / "x").mkdir(parents=True)
    (other / ".claude" / "skills" / "x" / "SKILL.md").write_text("x")
    chat_door.setup((other, []), ROOT, "v")
    assert (other / ".claude" / "skills" / "crypto-bot" / "SKILL.md").is_file()


def test_the_docker_godspeed_assistants_folder_is_found_through_the_containers_mounts(tmp_path):
    root = tmp_path / "opt-godspeed"
    (root / ".godspeed").mkdir(parents=True)
    (root / "compose.yaml").write_text("services: {}\n")
    (root / ".godspeed" / "model-host.json").write_text(json.dumps({
        "format": 1, "kind": "godspeed-docker", "service": "godspeed",
        "workspace": "/opt/data/full-candidate/workspace"}))
    gs = godspeed.find(str(root))
    volume = tmp_path / "volume" / "_data"
    (volume / "workspace").mkdir(parents=True)
    calls = []

    def run(argv, timeout=120, stdin=None):
        calls.append(argv)
        if argv[-3:] == ["ps", "-q", "godspeed"]:
            return 0, "abc123\n", ""
        if argv[:2] == ["docker", "inspect"]:
            return 0, json.dumps([{"Mounts": [
                {"Type": "volume", "Source": "/var/lib/docker/volumes/caddy/_data", "Destination": "/data"},
                {"Type": "volume", "Source": volume.as_posix(), "Destination": "/opt/data/full-candidate"}]}]), ""
        return 1, "", "unexpected"

    found = godspeed.assistant_root(gs, run)
    assert found == (Path(volume.as_posix()), ["workspace"])
    said = chat_door.setup(found, ROOT, "v")
    assert (volume / "workspace" / "crypto-bot" / "requests").is_dir()
    assert (volume / "workspace" / "skills" / "crypto-bot" / "SKILL.md").is_file()
    assert said
    assert godspeed.assistant_root(gs, lambda argv, timeout=120, stdin=None: (0, "", "")) is None  # not running


def test_the_relay_answers_the_chat_door_for_the_main_configuration_only(bed):
    bed.strategy()
    bed.request("r", {"request": "check", "strategy": "momo"})
    gs = godspeed.Godspeed(str(bed.mc))
    practice = godspeed.Relay(bed.cfg, gs, tell=lambda t: True, root=str(bed.tmp / "nowhere"))
    assert practice.chat_door() is None and not (bed.mc / "crypto-bot" / "requests" / "r.result.md").exists()
    main = godspeed.Relay(bed.cfg, gs, tell=lambda t: True, root=str(bed.tmp / "nowhere"),
                          config_path=bed.config_path, perps=lambda: set(PERPS), backtest=bed.fake_backtest)
    out = main()
    assert out["chat"] == ["r.json"]
    assert bed.answer("r")[1]["outcome"] == "done"


def test_the_backtest_runs_as_the_bots_user_and_its_report_is_read_without_links(tmp_path):
    cfg = config.Config(state_dir=str(tmp_path / "state")).validate()
    (tmp_path / "state" / "backtests").mkdir(parents=True)
    (tmp_path / "state" / "backtests" / "chat-x.md").write_bytes(REPORT.encode("utf-8"))
    seen = []

    def run(argv, timeout=120):
        seen.append((argv, timeout))
        return 0, "", "written"

    from datetime import date

    assert chat_door.run_backtest(cfg, "momo", date(2025, 1, 1), None, "chat-x", run=run) == REPORT
    assert seen[0][0] == ["/usr/local/sbin/godspeed-crypto-bot", "backtest", "file:momo", "--out-name", "chat-x",
                          "--start", "2025-01-01"]
    with pytest.raises(chat_door.Refused, match="There is not enough history"):
        chat_door.run_backtest(cfg, "momo", None, None, "chat-y", run=lambda a, timeout=0: (
            1, "", "godspeed-crypto-bot: There is not enough history to test this."))


def test_the_recipe_teaches_the_door_and_promises_nothing():
    text = (ROOT / "skill" / "crypto-bot" / "SKILL.md").read_text(encoding="utf-8")
    for must in ("crypto-bot/strategies/", "crypto-bot/requests/", '"request": "check"', '"request": "backtest"',
                 '"request": "use-on-paper"', "Never type or paste the confirmation sentence", "Never invent a setting",
                 "strategy-files.md", "say plainly", "worst"):
        assert must.lower() in text.lower(), must
    assert "—" not in text
    for line in text.splitlines():
        if re.search(r"\b(guarantee|will (make|earn|gain|return)|expected return|(?<!take-)profit)\w*", line, re.I):
            assert line.lstrip().startswith(("- **Never", "**Never")), line


@pytest.mark.skipif(not POSIX, reason="links are POSIX")
def test_a_godspeed_folder_reached_through_a_link_still_works(bed, tmp_path):
    os.symlink(bed.mc, tmp_path / "godspeed-link")
    bed.strategy()
    bed.request("via", {"request": "check", "strategy": "momo"})
    out = chat_door.Door(bed.cfg, bed.config_path, (tmp_path / "godspeed-link", []), lambda: set(PERPS),
                         backtest=bed.fake_backtest)()
    assert out["answered"] == ["via.json"] and bed.answer("via")[1]["outcome"] == "done"


def test_while_live_the_running_file_is_never_replaced_even_with_a_matching_fingerprint(tmp_path, monkeypatch):
    bed = Bed(tmp_path, mode="live")
    folder = tmp_path / "etc" / "strategies"
    folder.mkdir(parents=True)
    (folder / "momo.toml").write_text(MOMO, encoding="utf-8")
    bed.config_path.write_text(bed.config_path.read_text() + 'strategy = "file:momo"\n', encoding="utf-8")
    monkeypatch.setattr(strategy_file, "fingerprint", lambda data: "0" * 16)  # as if someone matched it
    bed.strategy(text=MOMO.replace("[20, 60]", "[10, 30]"))
    bed.request("swap", {"request": "check", "strategy": "momo"})
    bed.door()()
    text, result = bed.answer("swap")
    assert text.startswith('Refused: Crypto Bot trades real money with "momo" now, and this file is a changed version')
    assert (folder / "momo.toml").read_text(encoding="utf-8") == MOMO and result["outcome"] == "refused"
    bed.strategy(text=MOMO)  # the very same file is fine: nothing changes
    bed.request("same", {"request": "check", "strategy": "momo"})
    bed.door()()
    assert bed.answer("same")[1]["outcome"] == "done"


@pytest.mark.skipif(not POSIX, reason="links and owners are POSIX")
def test_a_link_of_another_user_never_leads_the_setup_into_roots_folders(tmp_path):
    link = tmp_path / "godspeed"
    os.symlink("/usr", link)  # a folder root owns
    if os.geteuid() == 0:  # run as root, the link is made as if the assistant's user had planted it
        os.lchown(link, 12345, 12345)
    with pytest.raises(chat_door.Refused, match="reached through a link of another user"):
        chat_door.setup((link, []), ROOT, "v")
    assert not os.path.exists("/usr/crypto-bot")


def test_an_existing_gitignore_gains_the_requests_rule_once(tmp_path):
    mc = tmp_path / "mc"
    (mc / "crypto-bot").mkdir(parents=True)
    (mc / "crypto-bot" / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    chat_door.setup((mc, []), ROOT, "v")
    lines = (mc / "crypto-bot" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "*.tmp" and lines.count("requests/") == 1
    chat_door.setup((mc, []), ROOT, "v")
    assert (mc / "crypto-bot" / ".gitignore").read_text(encoding="utf-8").splitlines().count("requests/") == 1
