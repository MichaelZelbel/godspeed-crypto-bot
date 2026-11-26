"""Uninstall: what it refuses, what it removes, what it keeps."""

from pathlib import Path

import pytest

from godspeed_crypto_bot import godspeed, uninstall
from godspeed_crypto_bot.names import NAME


class Market:
    def __init__(self, held=None, error=None):
        self.held, self.error = held or {}, error

    def open_holdings(self, address):
        if self.error:
            raise self.error
        return dict(self.held)


@pytest.fixture
def live(cfg):
    cfg.mode, cfg.master_address = "live", "0x" + "ab" * 20
    return cfg


def test_not_live_means_nothing_to_check(cfg):
    assert uninstall.check(cfg, market=Market({"perp:BTC": 1.0})) == {}


def test_it_refuses_while_the_account_holds_positions(live):
    with pytest.raises(uninstall.Refused) as exc:
        uninstall.check(live, market=Market({"perp:BTC": 0.01, "spot:UBTC": 0.01}))
    msg = str(exc.value)
    assert "BTC (mainnet) 0.01" in msg and f"{NAME} flatten --yes" in msg and "--yes to remove it anyway" in msg
    assert uninstall.check(live, yes=True, market=Market({"perp:BTC": 0.01})) == {"perp:BTC (mainnet)": 0.01}


def test_it_refuses_when_it_cannot_find_out_unless_told(live):
    with pytest.raises(uninstall.Refused, match="could not check Hyperliquid"):
        uninstall.check(live, market=Market(error=ConnectionError("down")))
    assert uninstall.check(live, yes=True, market=Market(error=ConnectionError("down"))) is None


def _tree(root):
    for p in (f"etc/systemd/system/{NAME}-daily.timer", f"etc/systemd/system/{NAME}-daily.service",
              f"etc/systemd/system/{NAME}-relay.timer", f"usr/local/sbin/{NAME}", f"opt/{NAME}/x",
              f"etc/{NAME}/config.toml", f"var/lib/{NAME}/bot.sqlite", f"var/lib/{NAME}-page/index.html",
              f"etc/credstore.encrypted/{NAME}.agent-key.mainnet"):
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")


def _run(cfg, tmp_path, **kw):
    calls, said = [], []
    uninstall.uninstall(cfg, root=str(tmp_path), run=lambda argv, **k: (calls.append(argv), (0, "", ""))[1],
                        say=said.append, **kw)
    return calls, said


def test_uninstall_removes_the_program_and_keeps_state_and_config(cfg, tmp_path):
    _tree(tmp_path)
    calls, said = _run(cfg, tmp_path)
    r = tmp_path
    assert not (r / f"opt/{NAME}").exists() and not (r / f"usr/local/sbin/{NAME}").exists()
    assert not list((r / "etc/systemd/system").glob(f"{NAME}-*"))
    assert (r / f"var/lib/{NAME}/bot.sqlite").exists() and (r / f"etc/{NAME}/config.toml").exists()
    assert (r / f"etc/credstore.encrypted/{NAME}.agent-key.mainnet").exists()
    assert ["systemctl", "disable", "--now", f"{NAME}-daily.timer"] in calls and ["userdel", NAME] in calls
    text = " ".join(said)
    assert "positions stay open" in text and "--purge" in text and "stays approved" in text


def test_purge_removes_everything_of_it(cfg, tmp_path):
    _tree(tmp_path)
    _calls, said = _run(cfg, tmp_path, purge=True)
    r = tmp_path
    for p in (f"var/lib/{NAME}", f"var/lib/{NAME}-page", f"etc/{NAME}", f"opt/{NAME}",
              f"etc/credstore.encrypted/{NAME}.agent-key.mainnet"):
        assert not (r / p).exists(), p
    assert "Purged" in " ".join(said)


def test_a_refused_uninstall_changes_nothing(live, tmp_path):
    _tree(tmp_path)
    with pytest.raises(uninstall.Refused):
        uninstall.uninstall(live, root=str(tmp_path), run=lambda *a, **k: pytest.fail("ran a command"),
                            market=Market({"perp:ETH": 1.0}))
    assert (tmp_path / f"opt/{NAME}/x").exists()


def test_the_page_address_comes_out_of_godspeeds_caddy(tmp_path):
    folder = tmp_path / "etc/caddy/godspeed-addons/paths"
    folder.mkdir(parents=True)
    (folder / f"{NAME}.caddy").write_text("x")
    (tmp_path / "etc/caddy/godspeed-notebook.caddy").write_text("srv1.example.com {\n}\n")
    calls = []
    godspeed.remove_page(godspeed.Godspeed(str(tmp_path)), lambda argv, **k: (calls.append(argv), (0, "", ""))[1],
                         str(tmp_path))
    assert not (folder / f"{NAME}.caddy").exists()


def test_the_account_is_checked_even_when_the_mode_was_switched_back(cfg, tmp_path):
    """Switching a live installation to paper (or clearing the address) must not skip the open-positions check."""
    from godspeed_crypto_bot import keys

    cfg.master_address = "0x" + "ab" * 20
    assert uninstall.check(cfg, market=Market({"perp:BTC": 1.0})) == {}  # never live: nothing to look for
    keys.address_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    keys.address_path(cfg).write_text("0x" + "cd" * 20)  # a key was made for it
    with pytest.raises(uninstall.Refused, match=r"BTC \(mainnet\) 1"):
        uninstall.check(cfg, market=Market({"perp:BTC": 1.0}))


def test_a_unit_that_will_not_stop_means_nothing_is_removed(cfg, tmp_path):
    _tree(tmp_path)

    def run(argv, **k):
        return (1, "", "busy") if argv[:3] == ["systemctl", "stop", f"{NAME}-daily.service"] else (0, "", "")

    with pytest.raises(uninstall.Refused, match="would not stop"):
        uninstall.uninstall(cfg, root=str(tmp_path), run=run, say=lambda *a: None)
    assert (tmp_path / f"opt/{NAME}/x").exists() and (tmp_path / f"etc/systemd/system/{NAME}-daily.timer").exists()
    # and the timers it had already switched off are on again: a refusal changes nothing
    calls = []

    def run2(argv, **k):
        calls.append(argv)
        return run(argv)

    with pytest.raises(uninstall.Refused):
        uninstall.uninstall(cfg, root=str(tmp_path), run=run2, say=lambda *a: None)
    for unit in (f"{NAME}-daily.timer", f"{NAME}-relay.timer"):
        if (tmp_path / f"etc/systemd/system/{unit}").exists():
            assert ["systemctl", "disable", "--now", unit] in calls and ["systemctl", "enable", "--now", unit] in calls


def test_a_user_that_cannot_be_removed_is_said_not_hidden(cfg, tmp_path):
    _tree(tmp_path)
    said = []

    def run(argv, **k):
        return (1, "", "user is logged in") if argv[:1] == ["userdel"] else (0, "", "")

    with pytest.raises(uninstall.Refused, match="not complete"):
        uninstall.uninstall(cfg, root=str(tmp_path), run=run, say=said.append)
    assert "could not be removed" in said[0] and "user is logged in" in said[0]


def test_uninstall_removes_only_the_container_this_add_on_made(tmp_path):
    (tmp_path / ".godspeed").mkdir()
    gs = godspeed.Godspeed(str(tmp_path), {"compose": "c", "service": "godspeed", "workspace": "/w"})

    def run_with(label):
        calls = []

        def run(argv, **k):
            calls.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                return 0, '[{"Config": {"Labels": {"org.godspeed.addon": "%s"}}}]' % label, ""
            return 0, "", ""

        return run, calls

    run, calls = run_with(NAME)
    godspeed.remove_page(gs, run, str(tmp_path))
    assert ["docker", "rm", "-f", godspeed.DOCKER_FRONT] in calls
    run, calls = run_with("something-else")
    godspeed.remove_page(gs, run, str(tmp_path))
    assert not any(c[:2] == ["docker", "rm"] for c in calls)


def test_the_other_network_is_checked_too(live):
    """A configuration switched from mainnet to testnet still leaves the mainnet positions open."""
    from godspeed_crypto_bot import keys

    live.network = "testnet"
    keys.address_path(live, "mainnet").parent.mkdir(parents=True, exist_ok=True)
    keys.address_path(live, "mainnet").write_text("0x" + "cd" * 20)  # a mainnet key exists
    seen = []

    class PerNetwork:
        def __init__(self, network):
            self.network = network

        def open_holdings(self, address):
            seen.append(self.network)
            return {"perp:ETH": 2.0} if self.network == "mainnet" else {}

    import godspeed_crypto_bot.venue.hyperliquid as hl

    original = hl.HyperliquidMarketData
    hl.HyperliquidMarketData = lambda network, tokens=None, *a, **k: PerNetwork(network)
    try:
        with pytest.raises(uninstall.Refused, match=r"ETH \(mainnet\) 2"):
            uninstall.check(live)
    finally:
        hl.HyperliquidMarketData = original
    assert "mainnet" in seen and "testnet" in seen


def test_the_other_network_is_asked_with_its_own_account(live, tmp_path):
    """A testnet track with its own configuration has its own account, and that is the one that is looked at."""
    from godspeed_crypto_bot import keys

    live.confirm_dir = str(tmp_path)
    live.network = "mainnet"
    other = "0x" + "ef" * 20
    (tmp_path / "config.testnet.toml").write_text(f'network = "testnet"\nmaster_address = "{other}"\n', encoding="utf-8")
    keys.address_path(live, "testnet").parent.mkdir(parents=True, exist_ok=True)
    keys.address_path(live, "testnet").write_text("0x" + "cd" * 20)
    asked = []

    class Market:
        def open_holdings(self, address):
            asked.append(address)
            return {"perp:ETH": 3.0} if address == other else {}

    with pytest.raises(uninstall.Refused, match=r"ETH \(testnet\) 3"):
        uninstall.check(live, market=Market())
    assert other in asked


@pytest.mark.parametrize("purge", [False, True])
def test_a_reminder_made_in_godspeed_is_named_because_only_the_owner_can_close_it(cfg, tmp_path, purge):
    _tree(tmp_path)
    _calls, said = _run(cfg, tmp_path, purge=purge)
    assert "reminder" not in " ".join(said)  # no reminder was ever made: nothing to say
    _tree(tmp_path)
    # the configured relay folder, under the test's root the way uninstall reads every path
    synced = Path(str(tmp_path)) / str(cfg.relay_path).lstrip("/") / "deadline-synced.json"
    synced.parent.mkdir(parents=True, exist_ok=True)
    synced.write_text("{}")
    _calls, said = _run(cfg, tmp_path, purge=purge)
    assert "exchange key stays among your Godspeed reminders; close it there" in " ".join(said)
