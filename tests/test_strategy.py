"""The pure decision function: hand-checked cases, causality, scaling, no hidden state."""

from datetime import date, timedelta

import numpy as np
import pytest

from godspeed_crypto_bot.data import build_history, funding_through_close
from godspeed_crypto_bot.strategy import (
    Books,
    History,
    decide,
    monthly_reset,
    needs_trade,
    select_hl,
    trend_signal,
)

from .conftest import TODAY, FakeMarket


def _history(n=200, start="2026-01-01", drift=0.002, funding=None):
    dates = np.arange(np.datetime64(start, "D"), np.datetime64(start, "D") + np.timedelta64(n, "D"))
    k = np.arange(n)[:, None]
    base = np.array([[80000.0, 2500.0, 100.0]])
    close = base * np.exp(drift * k + 0.01 * np.sin(k * np.array([[1.0, 1.3, 1.7]])))
    open_ = np.vstack([close[:1], close[:-1]])
    traded = np.ones_like(close, dtype=bool)
    f = np.full_like(close, 0.0003) if funding is None else funding
    return History(dates, ("BTC", "ETH", "SOL"), open_, np.maximum(open_, close), np.minimum(open_, close), close,
                   traded, open_, np.maximum(open_, close), np.minimum(open_, close), close, traded, f,
                   [np.datetime64("2020-01-01")] * 3, [True, True, True])


def test_trend_signal_by_hand():
    close = np.array([1.0] * 121 + [2.0])
    seg = np.zeros(122)
    assert trend_signal(close, seg, 121, (20, 60, 120)) == 1.0
    assert trend_signal(close, seg, 100, (20, 60, 120)) is None or trend_signal(close, seg, 100, (20, 60, 120)) == 0
    seg2 = np.full(122, 50.0)
    assert trend_signal(close, seg2, 121, (20, 60, 120)) is None  # look-back leaves the continuous run


def test_band_and_hysteresis_by_hand():
    assert needs_trade(0.5, 0.49, True, 0.02) is False
    assert needs_trade(0.5, 0.48, True, 0.02) is True
    assert needs_trade(0.1, -0.1, True, 0.02) is True  # sign change
    assert needs_trade(0.0, 0.3, False, 0.02) is True  # left the universe
    active = [0]
    assert select_hl(active, {0: 0.01, 1: 0.079, 2: 0.08}, {0: True, 1: True, 2: True}, [0, 1, 2], 0.08, 0.0) == [0, 2]
    assert select_hl(active, {0: -0.01, 1: 0.2, 2: 0.2}, {0: True, 1: False, 2: True}, [0, 1, 2], 0.08, 0.0) == [2]


def test_decide_trend_long_and_carry_on_high_funding():
    h = _history()
    books = Books.fresh(2000.0, date(2026, 7, 20))
    dec = decide(h, books)
    assert dec.day == h.last_day + timedelta(days=1)
    assert set(dec.trend_orders) == {"BTC", "ETH"} and all(q > 0 for q in dec.trend_orders.values())
    # 0.0003 a day = 10.95% a year >= 8%: every carry coin enters, 2/3 of the carry book shared equally
    assert dec.carry_active == ["BTC", "ETH", "SOL"]
    price = h.perp_close[-1]
    for i, c in enumerate(("BTC", "ETH", "SOL")):
        assert dec.carry_orders[c] * price[i] == pytest.approx(1000.0 * 2 / 3 / 3)
    gross = sum(abs(q * price[i]) for i, q in enumerate(dec.books_after.trend.q.values()))
    assert gross <= 2.0 * 1000.0 + 1e-9  # the trend book's gross cap


def test_decide_does_not_touch_its_inputs_and_scales_linearly():
    h = _history()
    books = Books.fresh(2000.0, date(2026, 7, 20))
    before = books.to_dict()
    d1 = decide(h, books)
    d2 = decide(h, books, equity=1000.0)
    assert books.to_dict() == before
    assert d2.scale == pytest.approx(0.5)
    for c, (p, s) in d1.account_targets().items():
        p2, s2 = d2.account_targets()[c]
        assert p2 == pytest.approx(p / 2) and s2 == pytest.approx(s / 2)


def test_decide_refuses_a_history_that_does_not_end_yesterday():
    h = _history()
    with pytest.raises(ValueError):
        decide(h, Books.fresh(2000.0, None), day=h.last_day + timedelta(days=2))


def test_carry_exits_below_zero_and_resets_after_a_15_percent_move():
    funding = np.full((200, 3), 0.0003)
    h = _history(funding=funding)
    d1 = decide(h, Books.fresh(2000.0, None))
    after = d1.books_after
    for c in after.carry.perp_q:
        after.carry.balance_price[c] = float(h.perp_close[-1][h.index[c]])
    funding2 = funding.copy()
    funding2[-7:, 0] = -0.0001  # BTC turns negative
    h2 = _history(funding=funding2)
    d2 = decide(h2, after)
    assert d2.carry_orders.get("BTC") == 0.0 and "BTC" not in d2.carry_active
    # the remaining coins are re-sized because the active set changed
    assert set(d2.carry_orders) == {"BTC", "ETH", "SOL"}
    same = decide(h, after)
    assert same.carry_orders == {}  # nothing moved, nothing changed: no carry trade
    after.carry.balance_price["SOL"] = float(h.perp_close[-1][2]) / 1.2
    moved = decide(h, after)
    assert set(moved.carry_orders) == {"SOL"}


def test_monthly_reset_only_after_the_first_day():
    b = Books.fresh(2000.0, date(2026, 9, 1))
    b.trend.equity, b.carry.equity = 1200.0, 900.0
    assert not monthly_reset(b, date(2026, 9, 1), 1e-4)
    assert monthly_reset(b, date(2026, 10, 1), 0.0)
    assert b.trend.equity == b.carry.equity == 1050.0


def test_history_through_equals_rebuilding_from_the_raw_rows():
    h = _history(n=180)
    day = h.last_day - timedelta(days=30)
    cut = h.through(day)
    k = len(cut.dates)
    rebuilt = History(h.dates[:k], h.coins, *(h.raw[n][:k] for n in (
        "perp_open", "perp_high", "perp_low", "perp_close", "perp_traded", "spot_open", "spot_high", "spot_low",
        "spot_close", "spot_traded", "funding")), h.first_trade, h.has_spot)
    for name in ("perp_close", "perp_valid", "spot_valid", "rates", "perp_segment", "log_ret"):
        np.testing.assert_array_equal(getattr(cut, name), getattr(rebuilt, name))
    assert decide(cut, Books.fresh(2000.0, None)).to_dict() == decide(rebuilt, Books.fresh(2000.0, None)).to_dict()


def test_funding_days_follow_the_labs_through_close_rule():
    d0 = date(2026, 10, 10)
    t = 1791590400000  # 2026-10-10 00:00 UTC
    ev = [(t + 12, 0.1), (t + 3_600_000 + 40, 0.2), (t + 86_400_000 + 20, 0.4)]
    out = funding_through_close(ev, d0 - timedelta(days=1), d0 + timedelta(days=1))
    # the 00:00 settlement belongs to the day that just closed; the next 00:00 one to 2026-10-10
    assert out[d0 - timedelta(days=1)] == (0.1, 1)
    assert out[d0] == pytest.approx((0.6, 2))


def test_build_history_from_venue_data_decides():
    m = FakeMarket()
    h, snap = build_history(m, TODAY, history_days=400)
    assert h.last_day == TODAY - timedelta(days=1)
    assert set(snap.open_perp) == {"BTC", "ETH", "SOL"}
    dec = decide(h, Books.fresh(2000.0, TODAY))
    assert dec.trend_orders  # rising prices: trend long
    assert dec.carry_active == ["SOL"]  # 0.000012 an hour = 10.5% a year; BTC and ETH 3.5%
