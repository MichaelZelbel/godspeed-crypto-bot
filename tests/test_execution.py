"""Order planning and execution: netting, the $11 floor, caps, carry pairs, leg failure and unwind."""

import pytest

from godspeed_crypto_bot.execution import Executor, plan_orders
from godspeed_crypto_bot.strategy import Books, Decision
from godspeed_crypto_bot.venue.base import AccountState, OrderResult, Position

from .conftest import INSTRUMENTS, TODAY


def _acct(perp=None, spot=None):
    positions = {c: Position(c, q) for c, q in (perp or {}).items()}
    return AccountState(2000.0, 2000.0, 0.0, 0.0, positions, {"USDC": (0.0, 0.0), **(spot or {})}, "simulated")


def _decision(trend_q, carry_spot):
    b = Books.fresh(2000.0, None)
    b.trend.q.update(trend_q)
    for c, q in carry_spot.items():
        b.carry.spot_q[c], b.carry.perp_q[c] = q, -q
    return Decision(TODAY, False, 1000.0, 1000.0, {}, {}, [], {}, {}, {}, 1.0, b, [])


def test_netting_puts_both_books_into_one_perp_position(cfg, market):
    dec = _decision({"BTC": 0.010}, {"BTC": 0.004, "SOL": 3.0})
    targets = dec.account_targets()
    assert targets["BTC"] == pytest.approx((0.006, 0.004))  # trend long 0.010, carry short 0.004
    assert targets["SOL"] == pytest.approx((-3.0, 3.0))
    plan = plan_orders(targets, _acct(), INSTRUMENTS, market.mids(), cfg)
    perp = {leg.coin: leg.size for leg in plan.legs if leg.market == "perp"}
    assert perp["BTC"] == pytest.approx(0.006)  # one order for the net, not two
    assert {leg.pair for leg in plan.legs if leg.coin == "SOL"} == {"SOL-pair"}


def test_orders_under_11_dollars_are_skipped_and_recorded(cfg, market):
    mids = market.mids()
    tiny = 10.0 / mids["ETH"]
    plan = plan_orders({"ETH": (tiny, 0.0), "BTC": (0.002, 0.0)}, _acct(), INSTRUMENTS, mids, cfg)
    assert [leg.coin for leg in plan.legs] == ["BTC"]
    assert plan.skipped and plan.skipped[0][0] == "ETH" and "under $11" in plan.skipped[0][4]


def test_sizes_round_down_to_the_venue_step(cfg, market):
    plan = plan_orders({"SOL": (1.23999, 0.0)}, _acct(), INSTRUMENTS, market.mids(), cfg)
    assert plan.legs[0].size == 1.23


def test_per_order_cap_clips_both_legs_of_a_pair_alike(cfg, market):
    cfg.limits.max_order_usdc = 500.0
    mids = market.mids()
    q = 1000.0 / mids["SOL"]
    plan = plan_orders({"SOL": (-q, q)}, _acct(), INSTRUMENTS, mids, cfg)
    assert plan.clipped
    for leg in plan.legs:
        assert abs(leg.size) * leg.mid <= 500.0 + 1e-6
    assert abs(plan.legs[0].size) == pytest.approx(abs(plan.legs[1].size), rel=0.01)


def test_daily_notional_cap_stops_further_orders(cfg, market, store, paper):
    cfg.limits.max_day_usdc = 300.0
    plan = plan_orders({"BTC": (0.003, 0.0), "ETH": (0.1, 0.0)}, paper.account(), INSTRUMENTS, market.mids(), cfg)
    ex = Executor(cfg, paper, store, TODAY, sleep=lambda s: None)
    ex.execute_plan(plan)
    statuses = [r["status"] for r in store.db.execute("SELECT status FROM orders ORDER BY id")]
    assert "stopped_day_cap" in statuses
    assert store.traded_notional_on(TODAY) <= 300.0


class LegFails:
    """Paper venue whose spot orders never fill; optionally unwinds fail too."""

    def __init__(self, inner, fail_unwind=False):
        self.inner, self.fail_unwind = inner, fail_unwind
        self.market = inner.market
        self.signs = False
        self.batches = []

    def place_ioc_batch(self, orders):
        self.batches.append(orders)
        return [self.place_ioc(*o) for o in orders]

    def place_ioc(self, inst, is_buy, size, limit_px, cloid, reduce_only=False):
        if inst.market == "spot" and is_buy:
            return OrderResult("unfilled", size, error="Order could not immediately match against any resting orders.")
        if self.fail_unwind and inst.market == "perp" and is_buy:
            return OrderResult("unfilled", size, error="no liquidity")
        return self.inner.place_ioc(inst, is_buy, size, limit_px, cloid, reduce_only)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_carry_legs_go_as_one_batch_and_a_failed_leg_is_unwound(cfg, market, store, paper):
    v = LegFails(paper)
    q = 3.0
    plan = plan_orders({"SOL": (-q, q)}, paper.account(), INSTRUMENTS, market.mids(), cfg)
    ex = Executor(cfg, v, store, TODAY, sleep=lambda s: None)
    failures = ex.execute_plan(plan)
    assert len(v.batches[0]) == 2  # both legs in the first signed action
    assert failures and failures[0]["unwound"] and failures[0]["coin"] == "SOL"
    assert abs(paper.account().perp_size("SOL")) < 0.01  # the filled short was bought back
    statuses = {r["purpose"]: r["status"] for r in store.db.execute("SELECT purpose, status FROM orders")}
    assert statuses["unwind"] == "filled"


def test_an_unwind_that_fails_reports_the_residual(cfg, market, store, paper):
    v = LegFails(paper, fail_unwind=True)
    plan = plan_orders({"SOL": (-3.0, 3.0)}, paper.account(), INSTRUMENTS, market.mids(), cfg)
    failures = Executor(cfg, v, store, TODAY, sleep=lambda s: None).execute_plan(plan)
    assert failures and not failures[0]["unwound"]
    assert failures[0]["residual"] == {"perp:SOL": pytest.approx(-3.0)}


def test_retries_fill_the_remainder(cfg, market, store, paper):
    sol = INSTRUMENTS[("SOL", "perp")]
    m = market.mid["SOL"]
    market.book_override["SOL"] = ([(m * 0.9999, 1.0)], [(m * 1.0001, 1.0)])  # one SOL a time
    plan = plan_orders({"SOL": (2.5, 0.0)}, paper.account(), INSTRUMENTS, market.mids(), cfg)
    ex = Executor(cfg, paper, store, TODAY, sleep=lambda s: None)
    ex.execute_plan(plan)
    attempts = store.db.execute("SELECT attempt, filled FROM attempts ORDER BY id").fetchall()
    assert [a["attempt"] for a in attempts] == [1, 2, 3]
    assert paper.account().perp_size("SOL") == pytest.approx(2.5)
    assert sol.round_size(2.5) == 2.5


def test_fill_slippage_is_measured_against_the_decision_mid(cfg, market, store, paper):
    plan = plan_orders({"ETH": (0.1, 0.0)}, paper.account(), INSTRUMENTS, market.mids(), cfg)
    Executor(cfg, paper, store, TODAY, sleep=lambda s: None).execute_plan(plan)
    row = store.db.execute("SELECT slippage_bps, avg_px, decision_mid FROM orders").fetchone()
    assert row["slippage_bps"] == pytest.approx((row["avg_px"] / row["decision_mid"] - 1) * 1e4)
    assert 0 < row["slippage_bps"] < 25  # within the 0.25% IOC limit


def test_spot_usdc_beyond_the_reserve_reaches_perp_before_perp_orders(cfg, market, store):
    from godspeed_crypto_bot.execution import Leg, Plan

    class V:
        def __init__(self):
            self.moves = []

        def transfer_usdc(self, amount, to_perp):
            self.moves.append((amount, to_perp))
            return True

    v = V()
    ex = Executor(cfg, v, store, TODAY)
    perp = Leg("BTC", INSTRUMENTS[("BTC", "perp")], 0.01, 80_000.0)
    deposit = AccountState(0.0, 0.0, 0.0, 0.0, {}, {"USDC": (999.0, 0.0)}, "disabled")
    assert ex.balance_before_trading(Plan([perp], [], []), deposit, 2.0) == pytest.approx(-997.0)
    assert v.moves == [(997.0, True)]
    v.moves.clear()  # an ordinary day: only the reserve sits in spot, nothing moves
    usual = AccountState(2000.0, 2000.0, 0.0, 0.0, {}, {"USDC": (2.005, 0.0)}, "disabled")
    assert ex.balance_before_trading(Plan([perp], [], []), usual, 2.0) == 0.0 and v.moves == []
    spot_buy = Leg("SOL", INSTRUMENTS[("SOL", "spot")], 3.0, 100.0)  # spot buys still draw from perp
    ex.balance_before_trading(Plan([perp, spot_buy], [], []), usual, 2.0)
    assert v.moves and v.moves[0][1] is False
