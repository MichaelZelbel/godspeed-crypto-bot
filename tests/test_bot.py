"""The daily run, the guard and every way they stop: idempotency, resume, halt, flatten, caps, drawdown,
API wallet expiry, forced deleveraging (one-legged carry), the Unit peg, and alerts."""

from datetime import datetime, timedelta, timezone

import pytest

from godspeed_crypto_bot import bot, outbox, safety
from godspeed_crypto_bot.venue.base import OrderResult
from godspeed_crypto_bot.venue.simulated import SimulatedVenue

from .conftest import NOW, TODAY


def at(day, h=0, m=2):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def daily(cfg, venue, store, notifier, day=TODAY, **kw):
    return bot.run_daily(cfg, venue, store, notifier, now=at(day), **kw)


def n_orders(store):
    return store.db.execute("SELECT COUNT(*) FROM orders WHERE status NOT LIKE 'skipped%'").fetchone()[0]


def test_first_run_trades_records_everything_and_a_rerun_trades_nothing(cfg, store, notifier, paper):
    out = daily(cfg, paper, store, notifier)
    assert out["status"] == "complete"
    assert out["carry_active"] == ["SOL"]
    acct = paper.account()
    assert acct.perp_size("BTC") > 0 and acct.perp_size("ETH") > 0
    assert acct.perp_size("SOL") < 0 and acct.spot_size("USOL") > 0
    assert store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] >= 5
    assert store.books_on(TODAY) is not None
    assert {r["book"] for r in store.db.execute("SELECT book FROM equity")} >= {
        "account", "shadow_total", "shadow_trend", "shadow_carry", "account_after_trades"}
    placed = n_orders(store)
    again = daily(cfg, paper, store, notifier)
    assert again["status"] == "complete" and "nothing traded again" in again["note"]
    assert n_orders(store) == placed
    assert not notifier.calls.argv if hasattr(notifier, "calls") else True


class CrashOnce:
    """A venue that dies after its first order, once (a crashed run)."""

    def __init__(self, inner):
        self.inner, self.market, self.signs, self.crashed = inner, inner.market, False, False
        self.count = 0

    def place_ioc(self, *a, **kw):
        self.count += 1
        if self.count == 2 and not self.crashed:
            self.crashed = True
            raise ConnectionError("network gone")
        return self.inner.place_ioc(*a, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_crashed_run_resumes_with_the_same_decision_and_never_double_trades(cfg, store, notifier, paper):
    v = CrashOnce(paper)
    with pytest.raises(ConnectionError):
        daily(cfg, v, store, notifier)
    first = store.run(TODAY)
    assert first["status"] == "failed" and first["pre_state"]
    out = daily(cfg, v, store, notifier)
    assert out["status"] == "complete"
    assert store.run(TODAY)["attempts"] == 2
    targets = out["targets"]
    acct = paper.account()
    for coin in ("BTC", "ETH"):
        assert acct.perp_size(coin) == pytest.approx(targets[coin]["perp"], abs=2e-4)
    assert acct.spot_size("USOL") == pytest.approx(targets["SOL"]["spot"], abs=2e-3)


def test_halt_file_stops_all_trading(cfg, store, notifier, paper):
    bot.write_halt(cfg, "test")
    out = daily(cfg, paper, store, notifier)
    assert out["status"] == "halted" and n_orders(store) == 0


def test_capital_cap_ignores_money_above_2000(cfg, store, notifier, market):
    rich = SimulatedVenue(market, store, "book", start_usdc=5000.0, now=lambda: NOW.timestamp())
    out = daily(cfg, rich, store, notifier)
    assert out["strategy_equity"] == 2000.0
    assert store.get("start")["capital"] == 2000.0
    notional = sum(abs(p.notional) for p in rich.account().positions.values())
    assert notional < 2000.0 * 1.4


def test_a_late_scheduled_run_skips_the_day(cfg, store, notifier, paper):
    out = bot.run_daily(cfg, paper, store, notifier, now=at(TODAY, 9))
    assert out["status"] == "skipped" and n_orders(store) == 0


def test_deadline_check_alerts_when_the_run_did_not_finish(cfg, store, notifier, calls, paper):
    out = bot.check_deadline(cfg, store, notifier, now=at(TODAY, 0, 45))
    assert not out["ok"] and len(calls.argv) == 1
    daily(cfg, paper, store, notifier)
    calls.clear()
    assert bot.check_deadline(cfg, store, notifier, now=at(TODAY, 0, 45))["ok"] and not calls.argv


def test_flatten_closes_everything_and_halts(cfg, store, notifier, paper):
    daily(cfg, paper, store, notifier)
    residual = bot.flatten(cfg, paper, store, notifier, "test", TODAY)
    acct = paper.account()
    assert residual == {} and bot.halted(cfg)
    assert not acct.positions and acct.spot_size("USOL") == 0
    assert store.get("expected_holdings")["coins"]["SOL"] == {"perp": 0.0, "spot": 0.0}


def test_drawdown_of_25_percent_halts_flattens_and_alerts(cfg, store, notifier, calls, paper):
    daily(cfg, paper, store, notifier)
    paper.st["perp_cash"] -= 600.0  # a 30% loss of the 2,000 start
    paper._save()
    out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3))
    assert out["status"] == "halted" and bot.halted(cfg)
    assert not paper.account().positions
    assert any("25% loss" in t for t in calls.texts())


def test_an_alert_is_one_plain_message_marked_as_practice_unless_it_is_live_on_mainnet(cfg, store, calls):
    from godspeed_crypto_bot.notify import Notifier

    n = Notifier(cfg, store)
    assert n.alert("k1", "stopped for a reason", "The facts.", "Do this.") == 0
    assert n.alert("k1", "stopped for a reason", "The facts.", "Do this.") is None  # deduplicated
    assert calls.texts() == ["Crypto Bot (practice, dry-run on mainnet): stopped for a reason. The facts. Do this."]
    cfg.mode = "live"
    n.alert("k2", "stopped for a reason", "The facts.", "Do this.")
    assert calls.texts()[-1] == "Crypto Bot: stopped for a reason. The facts. Do this."
    lanes = [r["lane"] for r in store.db.execute("SELECT lane FROM alerts ORDER BY id")]
    assert lanes == ["practice", "owner"]


def test_an_alert_that_could_not_be_queued_is_not_counted_as_sent(cfg, store, monkeypatch):
    from godspeed_crypto_bot.notify import Notifier

    monkeypatch.setattr(outbox, "put_alert", lambda *a, **k: False)
    n = Notifier(cfg, store)
    assert n.alert("k", "s", "status", "act") == -1
    monkeypatch.undo()
    assert n.alert("k", "s", "status", "act") == 0  # the same alert is tried again


# ---- API wallet expiry


class SigningVenue:
    """A paper venue that claims to sign (live), with a controllable agent record and refusal."""

    signs = True

    def __init__(self, inner, valid_until=None, listed=True, refuse=None):
        self.inner, self.market = inner, inner.market
        self.valid_until, self.listed, self.refuse = valid_until, listed, refuse

    def agent_validity(self):
        return {"address": "0xabc", "listed": self.listed,
                "valid_until_ms": int(self.valid_until.timestamp() * 1000) if self.valid_until else None}

    def place_ioc(self, inst, is_buy, size, limit_px, cloid, reduce_only=False):
        if self.refuse:
            return OrderResult("rejected", size, error=self.refuse)
        return self.inner.place_ioc(inst, is_buy, size, limit_px, cloid, reduce_only)

    def sync_records(self, store, a, b):
        return {}

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_no_advance_expiry_reminders_only_the_hard_stop(cfg, store, notifier, calls, paper):
    """Godspeed's reminder does the reminding; the bot stays silent until the key can no longer sign."""
    exp = at(TODAY + timedelta(days=10), 12)
    v = SigningVenue(paper, valid_until=exp)
    halts = []
    for d in (TODAY, TODAY + timedelta(days=9), exp.date()):
        r = safety.check_agent_expiry(cfg, store, notifier, v, at(d, 0, 45), halts.append)
        assert r["status"] == "ok"
    assert not calls.argv and not halts
    r = safety.check_agent_expiry(cfg, store, notifier, v, at(exp.date(), 13), halts.append)
    assert r["status"] == "expired" and halts and len(calls.argv) == 1
    assert store.get("agent")["source"].startswith("venue")


def test_preflight_silently_refuses_live_with_less_than_14_days_left(cfg, store, notifier, calls, paper):
    v = SigningVenue(paper, valid_until=at(TODAY + timedelta(days=13), 23))
    assert "less than 14 days" in safety.expiry_problems(cfg, store, v, at(TODAY))[0]
    v.valid_until = at(TODAY + timedelta(days=15))
    assert safety.expiry_problems(cfg, store, v, at(TODAY)) == []
    assert not calls.argv  # silent either way


def test_agent_expiry_falls_back_to_the_approval_date(cfg, store, notifier, calls, paper):
    cfg.agent_approved_on = "2026-05-01"
    v = SigningVenue(paper, valid_until=None)
    r = safety.check_agent_expiry(cfg, store, notifier, v, at(TODAY), lambda r: None)
    assert r["expires"].startswith("2026-10-28") and r["status"] == "ok"  # 180 days after approval
    assert safety.expiry_problems(cfg, store, v, at(TODAY + timedelta(days=1))) != []  # 14 days left: not enough
    cfg.agent_approved_on = ""
    store.put("agent", {})
    assert "unknown" in safety.expiry_problems(cfg, store, v, at(TODAY))[0]
    assert not calls.argv


def test_a_pruned_agent_halts_and_alerts(cfg, store, notifier, calls, paper):
    v = SigningVenue(paper, valid_until=at(TODAY + timedelta(days=90)), listed=False)
    out = daily(cfg, v, store, notifier)
    assert out["status"] == "halted" and bot.halted(cfg) and len(calls.argv) == 1


def test_signing_refused_mid_run_halts_and_alerts(cfg, store, notifier, calls, paper):
    v = SigningVenue(paper, valid_until=at(TODAY + timedelta(days=90)),
                     refuse="User or API Wallet 0x1234 does not exist.")
    out = daily(cfg, v, store, notifier)
    assert out["status"] == "halted" and bot.halted(cfg)
    assert any("refused its API wallet" in t for t in calls.texts())
    attempts = store.db.execute("SELECT attempt FROM attempts").fetchall()
    assert [a["attempt"] for a in attempts] == [1, 1]  # the first signed action (one carry pair), then nothing


# ---- forced deleveraging and one-legged carry


def test_a_shrunk_carry_short_is_rehedged_and_reported(cfg, store, notifier, calls, paper):
    daily(cfg, paper, store, notifier)
    before = paper.account().perp_size("SOL")
    paper.st["positions"]["SOL"]["size"] = round(before / 2, 2)  # Hyperliquid closed half the short
    paper._save()
    out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3))
    assert out["holdings"]["status"] == "repaired"
    assert "re-shorted" in out["holdings"]["repaired"][0]["action"]
    assert paper.account().perp_size("SOL") == pytest.approx(before, abs=0.02)
    assert any("closed part of the SOL carry short" in t for t in calls.texts()) and not bot.halted(cfg)


def test_when_the_market_refuses_the_orphaned_spot_is_sold(cfg, store, notifier, calls, paper, market):
    daily(cfg, paper, store, notifier)
    spot_before = paper.account().spot_size("USOL")
    paper.st["positions"]["SOL"]["size"] = 0.0
    del paper.st["positions"]["SOL"]
    paper._save()
    m = market.mid["SOL"]
    market.book_override["SOL"] = ([(m * 0.5, 100.0)], [(m * 1.0001, 100.0)])  # no bids within 1%
    out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3))
    assert "sold" in out["holdings"]["repaired"][0]["action"]
    assert paper.account().spot_size("USOL") < spot_before * 0.05
    assert len(calls.argv) == 1


def test_an_unexplained_change_halts_and_alerts(cfg, store, notifier, calls, paper):
    daily(cfg, paper, store, notifier)
    paper.st["positions"]["BTC"]["size"] *= 3  # someone traded by hand
    paper._save()
    out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3))
    assert out["holdings"]["status"] == "halted" and bot.halted(cfg)
    assert any("do not explain" in t for t in calls.texts())
    # nothing was closed
    assert paper.account().perp_size("BTC") > 0


# ---- Unit bridge peg


def test_peg_gap_above_1_percent_for_15_minutes_blocks_new_carry(cfg, store, notifier, market, paper):
    t0 = NOW.timestamp()
    market.mid["@156"] = market.mid["SOL"] * 1.015
    s = safety.observe_peg(store, paper, t0)
    assert not s["SOL"]["blocked"]
    for k in (5, 10, 15):
        s = safety.observe_peg(store, paper, t0 + k * 60)
    assert s["SOL"]["blocked"] and not s["SOL"]["exit"]
    assert store.db.execute("SELECT COUNT(*) FROM peg_gaps WHERE coin='SOL'").fetchone()[0] == 4
    market.mid["@156"] = market.mid["SOL"]
    out = daily(cfg, paper, store, notifier)
    assert paper.account().spot_size("USOL") == 0 and "SOL: no new carry" in " ".join(out["restrictions"])
    assert out["carry_active"] == ["SOL"]  # the virtual books still follow the strategy


def test_peg_gap_above_3_percent_for_15_minutes_exits_the_carry(cfg, store, notifier, calls, market, paper):
    daily(cfg, paper, store, notifier)
    assert paper.account().spot_size("USOL") > 0
    market.mid["@156"] = market.mid["SOL"] * 0.96
    for k in (0, 5, 10, 15):
        out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3, k))
    assert out["peg"]["SOL"]["exit"] and "SOL" in out["peg_exit"]
    acct = paper.account()
    assert acct.spot_size("USOL") == 0 and acct.perp_size("SOL") == pytest.approx(0.0, abs=0.01)
    assert any("lost its peg" in t for t in calls.texts())


# ---- the margin guard


def _guard_actions_since(store, n):
    return [r["action"] for r in store.db.execute("SELECT action FROM guard_actions ORDER BY id")][n:]


def test_guard_moves_free_spot_usdc_to_perp_first(cfg, store, notifier, calls, paper):
    daily(cfg, paper, store, notifier)
    n = len(_guard_actions_since(store, 0))
    paper.st["perp_cash"] -= 1000.0  # the money moved to the spot balance: leverage far above 4x, no loss
    paper.st["spot_usdc"] += 1000.0
    paper._save()
    out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3))
    assert out["status"] == "fixed" and out["by"] == "transfer"
    assert _guard_actions_since(store, n) == ["transfer_to_perp"] and not calls.argv


def test_guard_shrinks_the_carry_pairs_when_there_is_no_cash(cfg, store, notifier, calls, paper, market):
    daily(cfg, paper, store, notifier)
    n = len(_guard_actions_since(store, 0))
    move = 900.0
    paper.st["perp_cash"] -= move  # value now sits in USOL instead: no free USDC anywhere
    paper.st["spot"]["USOL"] += move / market.mid["@156"]
    paper.st["positions"]["SOL"]["size"] = round(paper.st["positions"]["SOL"]["size"] - move / market.mid["SOL"], 2)
    paper._save()
    safety.snapshot_holdings(store, paper, paper.account(), "test: as if the bot held this")
    out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3))
    acts = _guard_actions_since(store, n)
    assert "shrink_pair" in acts
    assert out["status"] == "fixed" and out["by"] == "shrink" and not calls.argv
    # the pair shrank on both legs alike and stays hedged
    acct = paper.account()
    assert acct.spot_size("USOL") + acct.perp_size("SOL") == pytest.approx(0.0, abs=0.05)


def test_guard_alerts_when_it_cannot_fix_the_margin(cfg, store, notifier, calls, paper):
    daily(cfg, paper, store, notifier)
    paper.st["perp_cash"] -= 1150.0  # a trend-only problem: nothing to transfer, no carry to shrink
    paper.st["spot_usdc"] += 1150.0
    for tok in list(paper.st["spot"]):
        paper.st["spot"][tok] = 0.0
    paper.st["positions"].pop("SOL", None)
    paper.st["spot_usdc"] -= 1150.0  # the cash is gone too, but the start record is moved so no drawdown
    paper._save()
    start = store.get("start")
    start["account_value"] = bot.account_value(paper, paper.account())
    store.put("start", start)
    safety.snapshot_holdings(store, paper, paper.account(), "test")
    out = bot.guard(cfg, paper, store, notifier, now=at(TODAY, 3))
    assert out["status"] == "unfixed" and any("margin" in t for t in calls.texts())


# ---- a unified account (one USDC balance for spot and the perps)


@pytest.fixture
def unified(cfg, store, market):
    return SimulatedVenue(market, store, "book", start_usdc=2000.0, now=lambda: NOW.timestamp(), unified=True)


def test_a_unified_account_trades_the_same_targets_without_any_transfer(cfg, store, notifier, unified, tmp_path):
    out = daily(cfg, unified, store, notifier)  # transfer_usdc raises on a unified account
    assert out["status"] == "complete" and out["carry_active"] == ["SOL"]
    acct = unified.account()
    assert acct.unified and acct.perp_size("SOL") < 0 and acct.spot_size("USOL") > 0
    assert not [r for r in store.db.execute("SELECT action FROM guard_actions") if r["action"].startswith("transfer")]
    # The USOL was paid from the one balance, and the account is counted once: about 2000 less costs.
    assert acct.spot_usdc == pytest.approx(unified.st["perp_cash"])
    assert 1990 < bot.account_value(unified, acct) <= 2000
    # Same decision, same targets as on separate balances.
    from godspeed_crypto_bot import config, records

    cfg2 = config.Config(state_dir=str(tmp_path / "sep"), confirm_dir=str(tmp_path)).validate()
    store2 = records.Store(cfg2.db_path, cfg2.mode, cfg2.network)
    sep = SimulatedVenue(unified.market, store2, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())
    out2 = daily(cfg2, sep, store2, notifier)
    assert out2["targets"] == out["targets"]
    store2.close()


def test_the_guard_measures_leverage_against_the_unified_usdc_and_never_transfers(cfg, store, notifier, calls,
                                                                                unified, market):
    daily(cfg, unified, store, notifier)
    acct = unified.account()
    lev = acct.perp_notional / acct.perp_value
    assert lev < cfg.guard.trigger_leverage and acct.perp_value == pytest.approx(acct.usdc_equity)
    n = len(_guard_actions_since(store, 0))
    move = 900.0  # value now sits in USOL instead: little USDC left to carry the perps
    unified.st["perp_cash"] -= move
    unified.st["spot"]["USOL"] += move / market.mid["@156"]
    unified.st["positions"]["SOL"]["size"] = round(unified.st["positions"]["SOL"]["size"] - move / market.mid["SOL"], 2)
    unified._save()
    safety.snapshot_holdings(store, unified, unified.account(), "test: as if the bot held this")
    out = bot.guard(cfg, unified, store, notifier, now=at(TODAY, 3))
    acts = _guard_actions_since(store, n)
    assert "shrink_pair" in acts and not [a for a in acts if a.startswith("transfer")]
    assert out["status"] == "fixed" and out["by"] == "shrink" and not calls.argv
    after = unified.account()
    assert after.perp_notional / after.perp_value <= cfg.guard.target_leverage + 1e-9
