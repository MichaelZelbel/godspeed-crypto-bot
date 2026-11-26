"""The testnet self-test and the testnet-only settings, against a stateful fake of Hyperliquid's testnet.

No network. The SDK builds and signs the real actions; the fake keeps balances, fills IOC orders against its
books and refuses the user-signed actions the agent must not be able to sign.
"""

import itertools
import time

import eth_account
import pytest
from hyperliquid.utils.signing import (
    USD_SEND_SIGN_TYPES,
    WITHDRAW_SIGN_TYPES,
    recover_agent_or_user_from_l1_action,
    recover_user_from_user_signed_action,
)

from godspeed_crypto_bot import config, keys, records, selftest
from godspeed_crypto_bot.notify import Notifier
from godspeed_crypto_bot.venue.hyperliquid import HyperliquidMarketData, HyperliquidVenue

AGENT_KEY = "0x" + "22" * 32
AGENT = eth_account.Account.from_key(AGENT_KEY).address
MASTER = "0x" + "cd" * 20

META = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                     {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                     {"name": "SOL", "szDecimals": 2, "maxLeverage": 20}]}
# Testnet as found on 2026-10-17: no UBTC; UETH and USOL quote far from their perps; HYPE has a usable book.
SPOT_META = {
    "tokens": [{"name": "USDC", "szDecimals": 8, "weiDecimals": 8, "index": 0,
                "tokenId": "0xeb62eee3685fc4c43992febcd9e75443"},
               {"name": "PURR", "szDecimals": 0, "weiDecimals": 5, "index": 1, "tokenId": "0x01"},
               {"name": "HYPE", "szDecimals": 2, "weiDecimals": 8, "index": 1105, "tokenId": "0x02"},
               {"name": "UETH", "szDecimals": 4, "weiDecimals": 9, "index": 1242, "tokenId": "0x03"},
               {"name": "USOL", "szDecimals": 3, "weiDecimals": 8, "index": 1279, "tokenId": "0x04"}],
    "universe": [{"tokens": [1, 0], "name": "PURR/USDC", "index": 0},
                 {"tokens": [1105, 0], "name": "@1035", "index": 1035},
                 {"tokens": [1242, 0], "name": "@1137", "index": 1137},
                 {"tokens": [1279, 0], "name": "@1165", "index": 1165}],
}
BOOKS = {  # best bid, best ask
    "BTC": (81443.0, 81453.0), "ETH": (2410.5, 2410.9), "SOL": (103.11, 103.22),
    "@1137": (46.0, 125.55), "@1165": (8.0, None), "@1035": (40.268, 41.08), "PURR/USDC": (4.5342, 4.5795),
}
SPOT_TOKEN = {"@1137": "UETH", "@1165": "USOL", "@1035": "HYPE", "PURR/USDC": "PURR"}
FEE = 0.0007


class FakeTestnet:
    """Balances, positions, fills and ledger of one testnet account, plus the info endpoints the bot reads.

    mode "unifiedAccount" reports as Hyperliquid did on testnet 2026-10-17: the one USDC balance in the spot
    state (cash, hold = 10% of the perps' notional), the perp state with positions and margin and an
    accountValue that is only the reserved slice. "disabled": separate perp and spot USDC.
    """

    def __init__(self, mode="unifiedAccount", accept_forbidden=False, usdc_in_spot=None):
        self.mode = mode
        self.accept_forbidden = accept_forbidden
        self.agents = [{"address": AGENT, "name": "gmc-bot", "validUntil": int(time.time() * 1000) + 170 * 86400_000}]
        in_spot = mode == "unifiedAccount" if usdc_in_spot is None else usdc_in_spot
        self.perp_usdc = 0.0 if in_spot else 999.0  # separate balances only
        self.spot = {"USDC": 999.0} if in_spot else {}
        self.pos = {}  # coin -> [size, entry]
        self.fills, self.ledger, self.forbidden, self.actions = [], [], [], []
        self.ids = itertools.count(1000)
        self.asset_name = {i: u["name"] for i, u in enumerate(META["universe"])}
        self.asset_name.update({10000 + u["index"]: u["name"] for u in SPOT_META["universe"]})

    @property
    def unified(self):
        return self.mode == "unifiedAccount"

    # ---- info
    def meta(self):
        return META

    def spot_meta(self):
        return SPOT_META

    def extra_agents(self, user):
        return self.agents

    def post(self, path, payload):
        if payload.get("type") == "userAbstraction":
            return self.mode
        raise AssertionError(payload)

    def mid(self, name):
        bid, ask = BOOKS[name]
        return (bid + ask) / 2 if ask else bid

    def all_mids(self):
        return {name: str(self.mid(name)) for name in BOOKS}

    def l2_snapshot(self, name):
        bid, ask = BOOKS[name]
        return {"levels": [[{"px": str(bid), "sz": "1000", "n": 1}] if bid else [],
                           [{"px": str(ask), "sz": "1000", "n": 1}] if ask else []]}

    def meta_and_asset_ctxs(self):
        return [META, [{"markPx": str(self.mid(u["name"]))} for u in META["universe"]]]

    def upnl(self):
        return sum(q * (self.mid(c) - e) for c, (q, e) in self.pos.items())

    def notional(self):
        return sum(abs(q) * self.mid(c) for c, (q, e) in self.pos.items())

    def user_state(self, user):
        margin, maint = self.notional() / 20, self.notional() / 80
        value = 0.1 * self.notional() + self.upnl() if self.unified else self.perp_usdc + self.upnl()
        positions = [{"position": {"coin": c, "szi": str(q), "entryPx": str(e),
                                   "unrealizedPnl": str(q * (self.mid(c) - e)),
                                   "marginUsed": str(abs(q) * self.mid(c) / 20), "liquidationPx": None,
                                   "positionValue": str(abs(q) * self.mid(c))}}
                     for c, (q, e) in self.pos.items() if q]
        return {"assetPositions": positions,
                "marginSummary": {"accountValue": str(value), "totalMarginUsed": str(margin)},
                "withdrawable": "0.0" if self.unified else str(value - margin),
                "crossMaintenanceMarginUsed": str(maint)}

    def spot_user_state(self, user):
        hold = 0.1 * self.notional() if self.unified else 0.0
        out = {"balances": [{"coin": t, "total": str(v), "hold": str(hold if t == "USDC" else 0.0)}
                            for t, v in self.spot.items()]}
        if self.unified:
            out["tokenToAvailableAfterMaintenance"] = [[0, str(self.spot.get("USDC", 0.0) - self.notional() / 80)]]
        return out

    def user_fills_by_time(self, user, start, end=None):
        return [f for f in self.fills if start <= f["time"] <= (end or 2**62)]

    def user_funding_history(self, user, start, end=None):
        return []

    def user_non_funding_ledger_updates(self, user, start, end=None):
        return [r for r in self.ledger if start <= r["time"] <= (end or 2**62)]

    # ---- exchange
    def exchange_post(self, url_path, payload):
        a = payload["action"]
        self.actions.append(a["type"])
        now = int(time.time() * 1000)
        if a["type"] == "order":
            if len({o["a"] >= 10000 for o in a["orders"]}) > 1:  # as testnet answered on 2026-10-17
                return {"status": "ok", "response": {"type": "order", "data": {
                    "statuses": [{"error": "Order action has different asset classes."}]}}}
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [self._order(o, now) for o in a["orders"]]}}}
        if a["type"] == "agentSendAsset":
            assert not self.unified, "a unified account has nothing to transfer"
            amount = float(a["amount"])
            if a["destinationDex"] == "spot":
                self.perp_usdc -= amount
                self.spot["USDC"] = self.spot.get("USDC", 0.0) + amount
            else:
                self.spot["USDC"] -= amount
                self.perp_usdc += amount
            self.ledger.append({"time": now, "hash": f"0x{next(self.ids):064x}", "delta": {
                "type": "send", "user": MASTER, "destination": MASTER, "sourceDex": a["sourceDex"],
                "destinationDex": a["destinationDex"], "token": "USDC", "amount": a["amount"],
                "usdcValue": a["amount"]}})
            return {"status": "ok", "response": {"type": "default"}}
        if a["type"] == "updateLeverage":
            return {"status": "ok", "response": {"type": "default"}}
        if a["type"] in ("withdraw3", "usdSend"):
            types = WITHDRAW_SIGN_TYPES if a["type"] == "withdraw3" else USD_SEND_SIGN_TYPES
            primary = "HyperliquidTransaction:Withdraw" if a["type"] == "withdraw3" else "HyperliquidTransaction:UsdSend"
            signer = recover_user_from_user_signed_action(dict(a), payload["signature"], types, primary, False)
            self.forbidden.append((a["type"], signer, a["destination"]))
            if self.accept_forbidden:
                self._collateral(-float(a["amount"]))
                return {"status": "ok", "response": {"type": "default"}}
            return {"status": "err", "response": f"Must deposit before performing actions. User: {signer.lower()}"}
        raise AssertionError(a)  # agentSetAbstraction included: the bot must never send it

    def _collateral(self, delta):
        if self.unified:
            self.spot["USDC"] = self.spot.get("USDC", 0.0) + delta
        else:
            self.perp_usdc += delta

    def _order(self, o, now):
        name = self.asset_name[o["a"]]
        buy, sz, px = o["b"], float(o["s"]), float(o["p"])
        bid, ask = BOOKS[name]
        fill = ask if buy else bid
        if fill is None or (buy and px < fill) or (not buy and px > fill):
            return {"error": "Order could not immediately match against any resting orders. asset=" + str(o["a"])}
        oid = next(self.ids)
        if name in SPOT_TOKEN:
            token = SPOT_TOKEN[name]
            if buy:
                if self.spot.get("USDC", 0.0) < sz * fill:
                    return {"error": "Insufficient spot balance asset=" + str(o["a"])}
                self.spot["USDC"] -= sz * fill
                self.spot[token] = self.spot.get(token, 0.0) + sz * (1 - FEE)
                fee, fee_token = sz * FEE, token
            else:
                self.spot[token] -= sz
                self.spot["USDC"] = self.spot.get("USDC", 0.0) + sz * fill * (1 - FEE)
                fee, fee_token = sz * fill * FEE, "USDC"
        else:
            q, e = self.pos.get(name, [0.0, fill])
            if o["r"] and (q == 0 or (q > 0) == buy or sz > abs(q) + 1e-12):
                return {"error": "Reduce only order would increase position."}
            if not self.unified and self.perp_usdc + self.upnl() < (self.notional() + sz * fill) / 20:
                return {"error": "Insufficient margin to place order. asset=" + str(o["a"])}
            dq = sz if buy else -sz
            if q and (q > 0) != (dq > 0):  # reducing: realize the PnL on the closed part
                self._collateral(min(abs(dq), abs(q)) * (fill - e) * (1 if q > 0 else -1))
            elif q:
                e = (e * abs(q) + fill * sz) / abs(q + dq)
            else:
                e = fill
            new = round(q + dq, 10)
            if new:
                self.pos[name] = [new, e]
            else:
                self.pos.pop(name, None)
            fee, fee_token = sz * fill * 0.00045, "USDC"
            self._collateral(-fee)
        self.fills.append({"coin": name, "side": "B" if buy else "A", "px": str(fill), "sz": str(sz), "time": now,
                           "fee": str(fee), "feeToken": fee_token, "closedPnl": "0", "dir": "x",
                           "hash": f"0x{oid:064x}", "oid": oid, "tid": oid + 500_000, "cloid": o.get("c")})
        return {"filled": {"totalSz": str(sz), "avgPx": str(fill), "oid": oid}}


def make_testnet_venue(fake):
    market = HyperliquidMarketData("testnet", info=fake)
    v = HyperliquidVenue(market, MASTER, AGENT_KEY, allow_missing_spot=True)
    v.exchange.post = fake.exchange_post
    return v


@pytest.fixture
def tcfg(tmp_path):
    return config.Config(mode="live", network="testnet", master_address=MASTER, state_dir=str(tmp_path / "state"),
                         confirm_dir=str(tmp_path), credstore_dir=str(tmp_path / "credstore"),
                         credentials_dir=str(tmp_path / "creds"), allow_missing_spot=True).validate()


@pytest.fixture
def tstore(tcfg):
    s = records.Store(tcfg.db_path, tcfg.mode, tcfg.network)
    yield s
    s.close()


# ---- the testnet-only settings


def test_testnet_only_settings_are_refused_on_mainnet(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('network = "testnet"\nallow_missing_spot = true\n[spot_tokens]\nETH = "UETH"\nSOL = "USOL"\n')
    c = config.load(p)
    assert c.allow_missing_spot and c.spot_tokens == {"ETH": "UETH", "SOL": "USOL"}
    with pytest.raises(ValueError, match="testnet"):
        config.load(p, network="mainnet")
    p.write_text('allow_missing_spot = true\n')
    with pytest.raises(ValueError, match="testnet"):
        config.load(p)
    p.write_text('[spot_tokens]\nBTC = "WBTC"\nETH = "UETH"\nSOL = "USOL"\n')
    with pytest.raises(ValueError, match="only on testnet"):
        config.load(p)
    p.write_text('confirm_file = "/etc/godspeed-crypto-bot/LIVE-CONFIRMED"\n')
    with pytest.raises(ValueError, match="unknown setting confirm_file"):  # the old shared path is gone
        config.load(p)
    assert config.Config().confirm_file.as_posix() == "/etc/godspeed-crypto-bot/LIVE-CONFIRMED.mainnet"
    assert config.Config(network="testnet").confirm_file.name == "LIVE-CONFIRMED.testnet"
    with pytest.raises(ValueError, match="testnet"):
        HyperliquidVenue(HyperliquidMarketData("mainnet", info=FakeTestnet()), MASTER, AGENT_KEY,
                         allow_missing_spot=True)


def test_preflight_on_testnet_carries_only_coins_that_have_spot():
    fake = FakeTestnet(mode="unifiedAccount")
    v = make_testnet_venue(fake)
    assert ("BTC", "spot") not in v.market.instruments() and ("ETH", "spot") in v.market.instruments()
    assert v.preflight() == []
    assert v.preflight_notes and "BTC is not carried" in v.preflight_notes[0]
    strict = HyperliquidVenue(v.market, MASTER, AGENT_KEY)  # without the setting: refused as before
    strict.exchange.post = fake.exchange_post
    assert strict.preflight() == ["instruments missing on testnet: [('BTC', 'spot')]"]


def test_testnet_alerts_are_marked_as_practice(tcfg, tstore):
    from godspeed_crypto_bot import outbox

    Notifier(tcfg, tstore).alert("k", "summary", "status", "act")
    texts = [outbox.read_json(p)["text"] for p in outbox.waiting_alerts(tcfg)]
    assert len(texts) == 1 and texts[0].startswith("Crypto Bot (practice, ") and "testnet" in texts[0]


# ---- the self-test


def test_selftest_refuses_mainnet_before_loading_any_key(tmp_path, monkeypatch):
    from godspeed_crypto_bot import cli

    def boom(*a, **k):
        raise AssertionError("a key was loaded")

    monkeypatch.setattr(keys, "load_agent_key", boom)
    monkeypatch.setattr("shutil.which", lambda c: None)
    conf = tmp_path / "c.toml"
    conf.write_text(f'mode = "live"\nmaster_address = "{MASTER}"\nstate_dir = "{(tmp_path / "s").as_posix()}"\n'
                    f'confirm_dir = "{tmp_path.as_posix()}"\n')
    assert cli.main(["--config", str(conf), "selftest"]) == 2
    assert cli.main(["--config", str(conf), "--network", "testnet", "--mode", "paper", "selftest"]) == 2
    cfg = config.load(conf)
    assert "testnet only" in selftest.refusal(cfg)
    fake = FakeTestnet()
    mainnet = HyperliquidVenue(HyperliquidMarketData("mainnet", info=fake), MASTER, AGENT_KEY)
    cfg.network = "testnet"
    assert selftest.refusal(cfg, mainnet) == "the venue signs for mainnet"
    with pytest.raises(selftest.SelftestRefused):
        selftest.Selftest(cfg, mainnet, None)


@pytest.mark.parametrize("mode", ["unifiedAccount", "disabled"])
def test_selftest_runs_every_step_and_records_it(tcfg, tstore, mode):
    fake = FakeTestnet(mode=mode)
    v = make_testnet_venue(fake)
    out = selftest.run(tcfg, v, tstore, sleep=lambda s: None)
    steps = {s["step"]: s for s in out["steps"]}
    assert out["ok"], [(s["step"], s["name"]) for s in out["steps"] if not s["ok"]]
    assert sorted(steps) == [0, 1, 2, 3, 4, 5, 6, 7, 8]
    assert steps[1]["mode"] == mode and "agentSetAbstraction" not in fake.actions
    if mode == "unifiedAccount":  # 2: one USDC balance, nothing is transferred, anywhere
        assert "skipped" in steps[2] and "agentSendAsset" not in fake.actions
        assert steps[3]["funded_spot_usdc"] == 0 and steps[3]["swept_back_usdc"] == 0
    else:  # 2: agentSendAsset both ways, signed by the agent
        assert steps[2]["first"] == "perp->spot"
        assert steps[2]["spot_usdc_after_first"] == pytest.approx(15.0) and steps[2]["spot_usdc_after_back"] == 0
    # 3: the strategy's own spot tokens have no usable book on testnet; HYPE does. One signed batch, two legs.
    assert steps[3]["spot"]["token"] == "HYPE"
    assert [t["why"].split(" ")[0] for t in steps[3]["tried_first"]] == ["spread", "one-sided"]
    first = steps[3]["signed_batches"][0]
    assert [o["instrument"] for o in first["orders"]] == ["ETH", "@1035"] and first["orders"][1]["buy"] is True
    assert first["signed_actions"] == [{"market": "spot", "orders": 1}, {"market": "perp", "orders": 1}]
    assert all(r["status"] == "filled" for r in first["results"])
    assert steps[3]["eth_perp_left"] == 0 and steps[3]["dust_left"]["HYPE"] < 0.01
    hype = steps[3]["open"]["spot:HYPE"]
    assert hype["slippage_bps"] == pytest.approx((41.08 / ((40.268 + 41.08) / 2) - 1) * 1e4, abs=0.01)
    # 4: perp open and close
    assert steps[4]["open"]["filled"] == pytest.approx(0.00015) and steps[4]["btc_perp_left"] == 0
    assert steps[4]["open"]["slippage_bps"] is not None
    # 5: both refused, attributed to the agent's own address, nothing moved
    assert steps[5]["withdraw3"]["rejected"] and steps[5]["usdSend"]["rejected"]
    assert steps[5]["withdraw3"]["message"] == f"Must deposit before performing actions. User: {AGENT.lower()}"
    assert steps[5]["withdraw3"]["attributed_to_the_agents_own_address"]
    assert "can never withdraw from or send out of the account" in steps[5]["meaning"]
    assert [(k, s, d) for k, s, d in fake.forbidden] == [("withdraw3", AGENT, selftest.SINK),
                                                         ("usdSend", AGENT, selftest.SINK)]
    # 6: the venue's ids reach the records and the export
    assert steps[6]["fills_matching_selftest_cloids"] == len(fake.fills) == 6
    assert steps[6]["export_trade_rows_with_venue_ids"] == steps[6]["export_trade_rows"] == 6
    assert steps[6]["sample_trade"]["venue_tx_hash"].startswith("0x")
    # 7: testnet's Unit prices are refused by the peg guard; BTC has no spot at all
    assert steps[7]["refused"] == ["ETH", "SOL"] == steps[7]["gap_above_1pct"]
    assert steps[7]["coins_without_spot"] == ["BTC"]
    assert not fake.pos
    assert 0 < steps[8]["usdc_spent_on_fees_and_spreads"] < 1.0  # spreads and fees, counted once
    rows = tstore.selftest_steps(out["session"])
    assert [r["step"] for r in rows] == [0, 1, 2, 3, 4, 5, 6, 7, 8] and all(r["ok"] for r in rows)
    orders = tstore.db.execute("SELECT purpose, status FROM orders").fetchall()
    assert len(orders) == 6 and {o["purpose"] for o in orders} == {"selftest"}
    assert tstore.get("expected_holdings")["source"] == "after selftest"


@pytest.mark.parametrize("mode", ["portfolioMargin", "dexAbstraction", "default"])
def test_selftest_never_changes_the_mode_and_does_not_trade_in_an_unsupported_one(tcfg, tstore, mode):
    fake = FakeTestnet(mode=mode)
    out = selftest.run(tcfg, make_testnet_venue(fake), tstore, sleep=lambda s: None)
    steps = {s["step"]: s for s in out["steps"]}
    assert fake.mode == mode and not out["ok"] and not steps[1]["ok"]
    assert sorted(steps) == [0, 1, 5, 6, 7, 8] and steps[5]["ok"] and steps[7]["ok"]  # no trading
    assert not fake.fills and "agentSetAbstraction" not in fake.actions


def test_separate_balances_with_usdc_in_spot_move_it_to_perp_before_perp_orders(tcfg, tstore):
    fake = FakeTestnet(mode="disabled", usdc_in_spot=True)  # where a deposit leaves it
    out = selftest.run(tcfg, make_testnet_venue(fake), tstore, sleep=lambda s: None)
    steps = {s["step"]: s for s in out["steps"]}
    assert out["ok"], [(s["step"], s["name"]) for s in out["steps"] if not s["ok"]]
    assert steps[2]["first"] == "spot->perp"
    assert steps[3]["funded_spot_usdc"] < -900  # the spot USDC went to perp before the batch


def test_selftest_fails_loudly_if_hyperliquid_ever_accepts_an_agent_withdrawal(tcfg, tstore):
    fake = FakeTestnet(mode="unifiedAccount", accept_forbidden=True)
    out = selftest.run(tcfg, make_testnet_venue(fake), tstore, sleep=lambda s: None)
    step5 = next(s for s in out["steps"] if s["step"] == 5)
    assert not step5["ok"] and not step5["withdraw3"]["rejected"] and not out["ok"]
    assert step5["meaning"].startswith("NOT REFUSED")


def test_preflight_accepts_unified_and_separate_balances_and_nothing_else():
    for mode in ("unifiedAccount", "disabled"):
        assert make_testnet_venue(FakeTestnet(mode=mode)).preflight() == []
    for mode in ("portfolioMargin", "dexAbstraction", "default"):
        problems = make_testnet_venue(FakeTestnet(mode=mode)).preflight()
        assert len(problems) == 1 and repr(mode) in problems[0]
        assert "account-mode" not in problems[0] and "switch" not in problems[0]


def test_a_unified_account_is_read_from_the_spot_state(tcfg):
    """The payload Hyperliquid's testnet returned on 2026-10-17 with a 0.00015 BTC long open."""

    class Measured(FakeTestnet):
        def user_state(self, user):
            return {"marginSummary": {"accountValue": "1.222089", "totalNtlPos": "12.22089",
                                      "totalRawUsd": "-10.998801", "totalMarginUsed": "0.611044"},
                    "crossMaintenanceMarginUsed": "0.152761", "withdrawable": "0.0",
                    "assetPositions": [{"position": {"coin": "BTC", "szi": "0.00015", "entryPx": "81472.0",
                                                     "positionValue": "12.22089", "unrealizedPnl": "0.00009",
                                                     "liquidationPx": None, "marginUsed": "0.611044"}}]}

        def spot_user_state(self, user):
            return {"balances": [{"coin": "USDC", "token": 0, "total": "998.994591", "hold": "1.222089"},
                                 {"coin": "UETH", "token": 1242, "total": "0.5", "hold": "0.0"}],
                    "tokenToAvailableAfterMaintenance": [[0, "998.84183"]]}

    acct = make_testnet_venue(Measured(mode="unifiedAccount")).account()
    assert acct.unified and not acct.two_balances
    assert acct.perp_value == pytest.approx(998.994591 + 0.00009)  # the USDC plus unrealized PnL, not 1.22
    assert acct.perp_withdrawable == pytest.approx(998.84183) and acct.maint_margin_used == pytest.approx(0.152761)
    assert acct.perp_notional == pytest.approx(12.22089)
    assert acct.account_value({"UETH": 100.0}) == pytest.approx(998.994681 + 50.0)  # USDC counted once
    sep = make_testnet_venue(FakeTestnet(mode="disabled")).account()
    assert sep.two_balances and sep.perp_value == pytest.approx(999.0) and sep.usdc_equity == pytest.approx(999.0)


def test_account_mode_only_shows_the_mode(tcfg, tstore, monkeypatch):
    from godspeed_crypto_bot import bot, cli

    fake = FakeTestnet(mode="unifiedAccount")
    monkeypatch.setattr(bot, "build_venue", lambda cfg, store, market=None: make_testnet_venue(fake))
    monkeypatch.setattr("shutil.which", lambda c: None)
    conf = tcfg.state_path.parent / "t.toml"
    conf.write_text("\n".join(['mode = "live"', 'network = "testnet"', f'master_address = "{MASTER}"',
                               f'state_dir = "{tcfg.state_path.as_posix()}"', ""]))
    assert cli.main(["--config", str(conf), "account-mode"]) == 0 and fake.actions == []
    with pytest.raises(SystemExit):
        cli.main(["--config", str(conf), "account-mode", "--standard"])  # the bot never sets the mode


def test_selftest_respects_the_halt_file(tcfg, tstore):
    tcfg.state_path.mkdir(parents=True, exist_ok=True)
    tcfg.halt_path.write_text("x")
    with pytest.raises(selftest.SelftestRefused, match="HALT"):
        selftest.run(tcfg, make_testnet_venue(FakeTestnet()), tstore)


@pytest.mark.parametrize("mode", ["unifiedAccount", "disabled"])
def test_the_agent_signs_every_selftest_action(tcfg, tstore, mode):
    fake = FakeTestnet(mode=mode)
    v = make_testnet_venue(fake)
    sent = []
    real = fake.exchange_post

    def spy(url_path, payload):
        sent.append(payload)
        return real(url_path, payload)

    v.exchange.post = spy
    selftest.run(tcfg, v, tstore, sleep=lambda s: None)
    for p in sent:
        a = p["action"]
        if a["type"] in ("withdraw3", "usdSend"):
            continue
        signer = recover_agent_or_user_from_l1_action(a, p["signature"], None, p["nonce"], None, False)
        assert signer.lower() == AGENT.lower()


# ---- the checks judge what the selftest proves, not the state of the account around it


def test_spot_leg_is_sized_to_sell_back_above_the_minimum_after_fee_and_rounding(tcfg, tstore):
    st = selftest.Selftest(tcfg, make_testnet_venue(FakeTestnet()), tstore)
    purr = st.spot_instrument("PURR")  # whole-token lots, mid about 4.56
    mid = 4.5568
    assert purr.round_size(selftest._ceil_size(purr, selftest.SPOT_USD, mid) * 0.998) * mid < 11  # the old size
    size, sellable = selftest.spot_leg_size(purr, mid, 11.0)
    assert size == 4 and sellable == 3 and sellable * mid >= 11.0 * selftest.SELLBACK_MARGIN
    assert selftest.spot_leg_size(purr, 100.0, 11.0) is None  # one token is above the cap: refused, not oversized


def test_selftest_trades_purr_like_whole_token_lots_when_that_is_the_usable_book(tcfg, tstore):
    fake = FakeTestnet()
    out = selftest.run(tcfg, make_testnet_venue(fake), tstore, sleep=lambda s: None, spot_choice="PURR")
    step3 = next(s for s in out["steps"] if s["step"] == 3)
    assert step3["ok"] and step3["spot"]["token"] == "PURR" and step3["spot"]["size"] == 4
    assert out["ok"], [(s["step"], s["name"]) for s in out["steps"] if not s["ok"]]


def test_a_unified_account_holding_positions_passes_steps_5_and_8(tcfg, tstore):
    fake = FakeTestnet()
    fake.pos = {"BTC": [0.00238, 80000.0], "ETH": [-0.067, 2500.0]}  # left by an earlier run; PnL drifts
    before = dict(fake.pos)
    out = selftest.run(tcfg, make_testnet_venue(fake), tstore, sleep=lambda s: None)
    steps = {s["step"]: s for s in out["steps"]}
    assert out["ok"], [(s["step"], s["name"]) for s in out["steps"] if not s["ok"]]
    assert steps[8]["positions_before"] == {"BTC": 0.00238, "ETH": -0.067} and steps[8]["positions_changed"] == {}
    assert steps[8]["includes_drift_of_positions_held_before"] is True
    assert {c: q for c, (q, e) in fake.pos.items()} == {c: q for c, (q, e) in before.items()}


def test_step_5_ignores_equity_drift_but_not_a_withdrawal_in_the_ledger(tcfg, tstore):
    class Drifting(FakeTestnet):
        def user_state(self, user):  # equity moves between the two reads, as with open positions
            out = super().user_state(user)
            self.spot["USDC"] += 5.0  # equity really moves between reads (the unified equity is the spot USDC)
            return out

    out = selftest.run(tcfg, make_testnet_venue(Drifting()), tstore, sleep=lambda s: None)
    assert next(s for s in out["steps"] if s["step"] == 5)["ok"]

    class Leaky(FakeTestnet):
        def user_non_funding_ledger_updates(self, user, start, end=None):
            if "usdSend" not in self.actions:  # nothing yet when the baseline is read
                return []
            return [{"time": int(time.time() * 1000), "hash": "0x1", "delta": {
                "type": "withdraw", "usdc": "2.0", "nonce": 1, "fee": "1.0"}}]

    out = selftest.run(tcfg, make_testnet_venue(Leaky()), tstore, sleep=lambda s: None)
    step5 = next(s for s in out["steps"] if s["step"] == 5)
    assert not step5["ok"] and step5["ledger_entries_moving_money_out"] and not out["ok"]


def test_step_5_fails_when_the_withdrawable_usdc_falls_by_what_was_asked(tcfg, tstore):
    class Draining(FakeTestnet):
        def exchange_post(self, url_path, payload):
            out = super().exchange_post(url_path, payload)
            if payload["action"]["type"] in ("withdraw3", "usdSend"):
                self.drained = getattr(self, "drained", 0.0) + float(payload["action"]["amount"])
            return out

        def user_state(self, user):
            out = super().user_state(user)
            out["withdrawable"] = str(100.0 - getattr(self, "drained", 0.0))
            return out

    out = selftest.run(tcfg, make_testnet_venue(Draining(mode="disabled")), tstore, sleep=lambda s: None)
    step5 = next(s for s in out["steps"] if s["step"] == 5)
    assert not step5["ok"] and step5["withdrawable_drop"] == pytest.approx(3.0)


def test_step_8_fails_when_a_position_is_left_changed(tcfg, tstore):
    class Sticky(FakeTestnet):
        def _order(self, o, now):
            res = super()._order(o, now)
            if o["r"] and self.asset_name[o["a"]] == "BTC":  # the close never happens: the position stays
                self.pos["BTC"] = [0.00015, 80000.0]
            return res

    out = selftest.run(tcfg, make_testnet_venue(Sticky()), tstore, sleep=lambda s: None)
    step8 = next(s for s in out["steps"] if s["step"] == 8)
    assert not step8["ok"] and "BTC" in step8["positions_changed"]


def test_a_position_held_the_other_way_round_is_left_as_it_was_found(tcfg, tstore):
    fake = FakeTestnet()
    fake.pos = {"BTC": [-0.00238, 80000.0], "ETH": [0.067, 2500.0]}  # opposite to the selftest's own BTC long, ETH short
    out = selftest.run(tcfg, make_testnet_venue(fake), tstore, sleep=lambda s: None)
    assert out["ok"], [(s["step"], s["name"]) for s in out["steps"] if not s["ok"]]
    assert {c: q for c, (q, e) in fake.pos.items()} == {"BTC": -0.00238, "ETH": 0.067}


def test_step_5_is_not_proven_by_a_timeout_or_an_unreadable_ledger(tcfg, tstore):
    import requests

    class TimesOut(FakeTestnet):
        def exchange_post(self, url_path, payload):
            if payload["action"]["type"] == "withdraw3":
                raise requests.Timeout("no answer")
            return super().exchange_post(url_path, payload)

    out = selftest.run(tcfg, make_testnet_venue(TimesOut()), tstore, sleep=lambda s: None)
    step5 = next(s for s in out["steps"] if s["step"] == 5)
    assert not step5["ok"] and not step5["withdraw3"]["rejected"] and "indeterminate" in step5["withdraw3"]

    class NoLedger(FakeTestnet):
        def user_non_funding_ledger_updates(self, user, start, end=None):
            raise requests.ConnectionError("down")

    out = selftest.run(tcfg, make_testnet_venue(NoLedger()), tstore, sleep=lambda s: None)
    step5 = next(s for s in out["steps"] if s["step"] == 5)
    assert not step5["ok"] and "ledger_error" in step5


def test_step_5_counts_any_new_transfer_type_out_but_not_what_was_already_there(tcfg, tstore):
    class Late(FakeTestnet):
        def user_non_funding_ledger_updates(self, user, start, end=None):
            rows = [{"time": int(time.time() * 1000), "hash": "0xold", "delta": {"type": "withdraw"}}]
            if "usdSend" in self.actions:
                rows.append({"time": int(time.time() * 1000), "hash": "0xnew",
                             "delta": {"type": "spotTransfer", "destination": "0x" + "11" * 20}})
            return rows

    out = selftest.run(tcfg, make_testnet_venue(Late()), tstore, sleep=lambda s: None)
    step5 = next(s for s in out["steps"] if s["step"] == 5)
    assert [r["hash"] for r in step5["ledger_entries_moving_money_out"]] == ["0xnew"] and not step5["ok"]
