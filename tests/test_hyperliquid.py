"""The Hyperliquid adapter against a mocked exchange: what is signed, by whom, and what comes back.

No network. The SDK builds and signs the real actions; only the HTTP post is replaced.
"""

import eth_account
import pytest
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from godspeed_crypto_bot import bot, config, keys
from godspeed_crypto_bot.venue.hyperliquid import (
    HyperliquidMarketData,
    HyperliquidVenue,
    parse_order_response,
)
from godspeed_crypto_bot.venue.simulated import SimulatedVenue

AGENT_KEY = "0x" + "11" * 32
MASTER = "0x" + "ab" * 20

META = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                     {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                     {"name": "ATOM", "szDecimals": 2, "maxLeverage": 5},
                     {"name": "MATIC", "szDecimals": 1, "maxLeverage": 5},
                     {"name": "DYDX", "szDecimals": 1, "maxLeverage": 5},
                     {"name": "SOL", "szDecimals": 2, "maxLeverage": 20}]}
SPOT_META = {
    "tokens": [{"name": "USDC", "szDecimals": 8, "weiDecimals": 8, "index": 0,
                "tokenId": "0x6d1e7cde53ba9467b783cb7c530ce054"},
               {"name": "UBTC", "szDecimals": 5, "weiDecimals": 10, "index": 197, "tokenId": "0x8f25"},
               {"name": "UETH", "szDecimals": 4, "weiDecimals": 9, "index": 221, "tokenId": "0xe1ed"},
               {"name": "USOL", "szDecimals": 3, "weiDecimals": 8, "index": 254, "tokenId": "0x49b6"}],
    "universe": [{"tokens": [197, 0], "name": "@142", "index": 142},
                 {"tokens": [221, 0], "name": "@151", "index": 151},
                 {"tokens": [254, 0], "name": "@156", "index": 156}],
}


class FakeInfo:
    def __init__(self):
        self.agents = [{"address": eth_account.Account.from_key(AGENT_KEY).address, "name": "gmc-bot",
                        "validUntil": 1800000000000}]

    def meta(self):
        return META

    def spot_meta(self):
        return SPOT_META

    def extra_agents(self, user):
        return self.agents

    def post(self, path, payload):
        if payload.get("type") == "userAbstraction":
            return "disabled"
        raise AssertionError(payload)


@pytest.fixture
def venue():
    market = HyperliquidMarketData("mainnet", info=FakeInfo())
    v = HyperliquidVenue(market, MASTER, AGENT_KEY)
    v.sent = []

    def post(url_path, payload=None):
        v.sent.append(payload)
        action = payload["action"]
        if action["type"] == "order":
            statuses = []
            for o in action["orders"]:
                statuses.append({"filled": {"totalSz": o["s"], "avgPx": o["p"], "oid": 7}} if o["b"]
                                else {"error": "Order could not immediately match against any resting orders. asset=5"})
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
        return {"status": "ok", "response": {"type": "default"}}

    v.exchange.post = post
    return v


def test_instruments_come_from_the_venue_metadata(venue):
    insts = venue.market.instruments()
    assert insts[("SOL", "perp")].px_decimals == 4 and insts[("SOL", "perp")].max_leverage == 20
    assert insts[("BTC", "spot")].name == "@142" and insts[("BTC", "spot")].token == "UBTC"
    assert venue.market.usdc_token == "USDC:0x6d1e7cde53ba9467b783cb7c530ce054"


def test_a_carry_pair_is_two_signed_ioc_actions_back_to_back_spot_first(venue):
    """Hyperliquid refuses spot and perp orders in one action ("Order action has different asset classes.")."""
    insts = venue.market.instruments()
    cl1, cl2 = "0x" + "01" * 16, "0x" + "02" * 16
    res = venue.place_ioc_batch([
        (insts[("SOL", "perp")], False, 6.3, 106.2, cl1, False),
        (insts[("SOL", "spot")], True, 6.307, 106.9, cl2, False),
    ])
    first, second = venue.sent[-2:]
    assert [len(p["action"]["orders"]) for p in (first, second)] == [1, 1]
    (spot,), (perp,) = first["action"]["orders"], second["action"]["orders"]
    assert spot["a"] == 10000 + 156 and spot["b"] is True and spot["s"] == "6.307" and spot["c"] == cl2
    assert perp == {"a": 5, "b": False, "p": "106.2", "s": "6.3", "r": False, "t": {"limit": {"tif": "Ioc"}}, "c": cl1}
    assert venue.last_batch_actions == [{"market": "spot", "orders": 1}, {"market": "perp", "orders": 1}]
    for payload in (first, second):
        action = payload["action"]
        assert action["type"] == "order" and action["grouping"] == "na"
        assert payload["vaultAddress"] is None  # the agent trades for the master it is approved for
        signer = recover_agent_or_user_from_l1_action(action, payload["signature"], None, payload["nonce"], None,
                                                      True)
        assert signer.lower() == venue.agent_address.lower()
    assert [r.status for r in res] == ["unfilled", "filled"]  # results in the order the legs were given
    assert res[1].filled == pytest.approx(6.307) and res[1].avg_px == pytest.approx(106.9)


def test_prices_and_sizes_follow_the_venue_rules():
    from godspeed_crypto_bot.venue.base import Instrument
    btc = Instrument("BTC", "perp", "BTC", 5, 1)
    sol = Instrument("SOL", "perp", "SOL", 2, 4)
    assert btc.round_price(83456.78) == 83457.0
    assert sol.round_price(106.123456) == 106.12
    assert btc.round_size(0.0038000000000000004) == 0.0038
    assert sol.round_size(-1.239) == -1.23


def test_usdc_moves_between_spot_and_perp_with_an_agent_signed_send(venue):
    assert venue.transfer_usdc(123.45, to_perp=True)
    payload = venue.sent[-1]
    a = payload["action"]
    assert a["type"] == "agentSendAsset" and a["destination"] == MASTER.lower()
    assert (a["sourceDex"], a["destinationDex"]) == ("spot", "")
    assert a["token"] == "USDC:0x6d1e7cde53ba9467b783cb7c530ce054" and a["amount"] == "123.45"
    assert a["nonce"] == payload["nonce"]
    signer = recover_agent_or_user_from_l1_action(a, payload["signature"], None, payload["nonce"], None, True)
    assert signer.lower() == venue.agent_address.lower()
    venue.transfer_usdc(10, to_perp=False)
    assert (venue.sent[-1]["action"]["sourceDex"], venue.sent[-1]["action"]["destinationDex"]) == ("", "spot")


def test_leverage_is_set_cross_at_the_ceiling(venue):
    venue.set_leverage("BTC", 3)
    a = venue.sent[-1]["action"]
    assert a == {"type": "updateLeverage", "asset": 0, "isCross": True, "leverage": 3}


def test_agent_validity_and_preflight(venue):
    v = venue.agent_validity()
    assert v["listed"] and v["valid_until_ms"] == 1800000000000
    assert venue.preflight() == []
    venue.market.info.agents = []
    assert venue.agent_validity()["listed"] is False
    assert any("not approved" in p for p in venue.preflight())


def test_order_responses_are_parsed():
    ok = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "0.5", "avgPx": "10", "oid": 1}}]}}}
    assert parse_order_response(ok, 1.0).status == "partial"
    assert parse_order_response(ok, 0.5).status == "filled"
    no = {"status": "ok", "response": {"data": {"statuses": [{"error": "Order must have minimum value of $10."}]}}}
    assert parse_order_response(no, 1.0).status == "rejected"
    assert parse_order_response({"status": "err", "response": "User or API Wallet 0x1 does not exist."}, 1).status \
        == "rejected"


def test_dry_run_and_paper_never_build_a_signer(cfg, store, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a key was loaded")

    monkeypatch.setattr(eth_account.Account, "from_key", boom)
    monkeypatch.setattr(keys, "load_agent_key", boom)
    from .conftest import FakeMarket

    for mode in ("dry-run", "paper"):
        cfg.mode = mode
        v = bot.build_venue(cfg, store, market=FakeMarket())
        assert isinstance(v, SimulatedVenue) and v.signs is False


def test_live_mode_refuses_without_every_condition(cfg, store):
    from .conftest import FakeMarket

    cfg.mode = "live"
    with pytest.raises(keys.LiveNotAllowed) as e:
        bot.build_venue(cfg, store, market=FakeMarket())
    text = " ".join(e.value.problems)
    assert "master_address" in text and "agent key" in text and "confirmation file" in text


def test_config_refuses_unknown_settings_and_bad_values(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('mode = "live"\nnetwork = "testnet"\ncapital_cap_usdc = 2000\n[limits]\nleverage = 3\n')
    c = config.load(p)
    assert (c.mode, c.network, c.limits.leverage, c.max_order(), c.max_day()) == ("live", "testnet", 3, 2800.0, 8000.0)
    p.write_text('modee = "live"\n')
    with pytest.raises(ValueError):
        config.load(p)
    p.write_text('[limits]\nleverage = 20\n')
    with pytest.raises(ValueError):
        config.load(p)
    assert config.load(tmp_path / "missing.toml").mode == "dry-run"  # the default


def test_cli_refuses_live_without_the_gate_and_does_not_ask_systemd_to_retry(tmp_path, monkeypatch):
    from godspeed_crypto_bot import cli

    conf = tmp_path / "c.toml"
    conf.write_text(f'mode = "live"\nstate_dir = "{(tmp_path / "s").as_posix()}"\n'
                    f'confirm_dir = "{(tmp_path / "C").as_posix()}"\n')
    monkeypatch.setattr("shutil.which", lambda c: None)
    assert cli.main(["--config", str(conf), "run-daily"]) == cli.EXIT_CONFIG
    assert cli.main(["--config", str(conf), "status"]) == 0
