"""Strategy parity: every day of the research's hold-out, the bot's decide() must order what the research engine ordered.

The fixture is the research engine's own replay of "S3-MAJ | HL funding",
checked against the recorded hold-out evidence before it was written. For each day it holds what each
book's decide() saw; here the bot's pure function gets the same inputs and must return the same targets,
bit for bit.
"""

import gzip
import json
import math
import platform
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from godspeed_crypto_bot.strategy import (
    LAB_COMMIT,
    LAB_ENGINE_BLOB,
    Books,
    CarryBookState,
    History,
    TrendBookState,
    decide,
)

FIXTURE = Path(__file__).parent / "fixtures" / "s3maj_holdout_parity.json.gz"


@pytest.fixture(scope="module")
def doc():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def history(doc):
    h = doc["history"]
    n = len(h["perp_close"])
    dates = np.arange(np.datetime64(h["first_day"], "D"), np.datetime64(h["first_day"], "D") + np.timedelta64(n, "D"))
    return History(
        dates, h["coins"], h["perp_open"], h["perp_high"], h["perp_low"], h["perp_close"], h["perp_traded"],
        h["spot_open"], h["spot_high"], h["spot_low"], h["spot_close"], h["spot_traded"], h["funding"],
        h["first_trade"], h["has_spot"],
    )


def _same_platform(doc):
    g = doc.get("generated_on", {})
    return (g.get("system"), g.get("machine"), g.get("numpy")) == (platform.system(), platform.machine(), np.__version__)


def _equal(got, expected, exact):
    """The same coins with the same targets: bit for bit on the fixture's own platform; elsewhere numpy's
    vectorized math may round the last bit differently, so 1e-12 relative (the decisions must still match)."""
    if set(got) != set(expected):
        return False
    if exact:
        return got == expected
    return all(math.isclose(got[k], expected[k], rel_tol=1e-12, abs_tol=0.0) for k in got)


def _books(day, start_day, trend_equity, carry_equity):
    t, c = day["trend"], day["carry"]
    return Books(
        TrendBookState(trend_equity, dict(t["q"])),
        CarryBookState(carry_equity, dict(c["perp_q"]), dict(c["spot_q"]), list(c["active"]), list(c["last_set"]),
                       dict(c["balance_price"])),
        start_day,
    )


def test_parity_is_exact_where_the_fixture_was_made(doc):
    """On Windows/AMD64 with numpy 2.5.3 (where the research produced the fixture) the check is bit for bit."""
    assert doc["generated_on"]["numpy"] == "2.5.3"


def test_fixture_is_the_vendored_lab_code(doc):
    assert doc["lab_engine_blob"] == LAB_ENGINE_BLOB
    assert doc["lab_commit"] == LAB_COMMIT
    assert doc["replay_equals_recorded_daily_equity"] is True
    assert len(doc["days"]) == 1016  # 2024-01-01 .. 2026-10-12


def test_every_holdout_day_matches_the_lab(doc, history):
    """The equities here are the ones the research's books saw, i.e. after its monthly reset, so the books get no
    start day (no second reset); test_monthly_reset_matches_the_lab covers the reset itself."""
    mismatches, resets, traded_days = [], 0, 0
    exact = _same_platform(doc)
    for day in doc["days"]:
        d = date.fromisoformat(day["day"])
        books = _books(day, None, day["trend"]["equity"], day["carry"]["equity"])
        dec = decide(history.through(date.fromordinal(d.toordinal() - 1)), books)
        if not _equal(dec.trend_orders, day["trend"]["orders"], exact):
            mismatches.append((day["day"], "trend", dec.trend_orders, day["trend"]["orders"]))
        if not _equal(dec.carry_orders, day["carry"]["orders"], exact):
            mismatches.append((day["day"], "carry", dec.carry_orders, day["carry"]["orders"]))
        if dec.carry_active != day["carry"]["active_after"]:
            mismatches.append((day["day"], "active", dec.carry_active, day["carry"]["active_after"]))
        traded_days += bool(dec.trend_orders or dec.carry_orders)
        resets += dec.reset
    assert not mismatches, mismatches[:5]
    assert traded_days > 300  # the replay is not vacuous: the books trade on many days
    assert resets == 0  # equities were taken after the research's reset, so decide() must not reset again


def test_monthly_reset_matches_the_lab(doc, history):
    """From the previous day's closing equities, decide() itself applies the research's 50/50 reset."""
    start_day = date.fromisoformat(doc["start_day"])
    checked = 0
    exact = _same_platform(doc)
    for day in doc["days"]:
        d = date.fromisoformat(day["day"])
        if d.day != 1 or d == start_day:
            continue
        books = _books(day, start_day, day["trend"]["equity_end_prev"], day["carry"]["equity_end_prev"])
        dec = decide(history.through(date.fromordinal(d.toordinal() - 1)), books)
        assert dec.reset
        assert dec.trend_equity == day["trend"]["equity"]
        assert dec.carry_equity == day["carry"]["equity"]
        assert _equal(dec.trend_orders, day["trend"]["orders"], exact)
        assert _equal(dec.carry_orders, day["carry"]["orders"], exact)
        checked += 1
    assert checked == 33  # Feb 2024 .. Oct 2026


def test_shadow_ledger_reproduces_the_lab_equity_curve(doc, history):
    """The bot runs its own books forward through the hold-out (decide, fill at the open with the research's fees
    and slippage tiers, mark at the close, funding, the research's $5 per 30 days running cost) and must land on
    the research's recorded daily equity to the cent, every day."""
    from godspeed_crypto_bot.ledger import Shadow, ShadowState, slippage_for_rank

    start_day = date.fromisoformat(doc["start_day"])
    coins = doc["history"]["coins"]
    h = doc["history"]
    first = np.datetime64(h["first_day"], "D")

    def row(name, d):
        k = int((np.datetime64(d, "D") - first) // np.timedelta64(1, "D"))
        return {c: h[name][k][j] for j, c in enumerate(coins)}

    shadow = Shadow(ShadowState(Books.fresh(doc["start_equity"], start_day)))
    recorded = doc["recorded_daily_equity"]
    worst = 0.0
    for k, day in enumerate(doc["days"]):
        d = date.fromisoformat(day["day"])
        ranks = doc["ranks"][day["day"][:7]]
        dec = decide(history.through(date.fromordinal(d.toordinal() - 1)), shadow.s.books)
        shadow.execute(dec, row("perp_open", d), row("spot_open", d),
                       slippage=lambda c, ranks=ranks: slippage_for_rank(ranks[c]))
        shadow.settle(d, row("perp_close", d), row("spot_close", d), row("funding", d), running_per_day=5.0 / 30.0)
        worst = max(worst, abs(shadow.s.books.equity - recorded[k]))
    assert worst < 0.01, worst


# The two single-book choices. The research recorded only the S3-MAJ mix, so the reference for "trend" and "carry"
# is each book's own targets inside the research engine's S3-MAJ replay, with the whole strategy equity in that book
# (the books never look at each other, and sizing is linear in equity, so the research's TrendBook alone with that
# equity decides exactly this).
@pytest.mark.parametrize("strategy,book", [("trend", "trend"), ("carry", "carry")])
def test_single_book_choice_matches_the_labs_book_every_holdout_day(doc, history, strategy, book):
    from godspeed_crypto_bot.strategy import config_for

    cfg = config_for(strategy)
    exact = _same_platform(doc)
    mismatches, traded_days = [], 0
    for day in doc["days"]:
        d = date.fromisoformat(day["day"])
        te, ce = day["trend"]["equity"], day["carry"]["equity"]
        books = _books(day, None, te if strategy == "trend" else 0.0, ce if strategy == "carry" else 0.0)
        books.strategy = strategy
        if strategy == "trend":  # the book that does not run holds nothing
            books.carry = CarryBookState(0.0)
        else:
            books.trend = TrendBookState(0.0)
        dec = decide(history.through(date.fromordinal(d.toordinal() - 1)), books, config=cfg)
        ours, other = (dec.trend_orders, dec.carry_orders) if book == "trend" else (dec.carry_orders, dec.trend_orders)
        if not _equal(ours, day[book]["orders"], exact):
            mismatches.append((day["day"], ours, day[book]["orders"]))
        if other:
            mismatches.append((day["day"], "the other book ordered", other))
        if book == "carry" and dec.carry_active != day["carry"]["active_after"]:
            mismatches.append((day["day"], "active", dec.carry_active, day["carry"]["active_after"]))
        traded_days += bool(ours)
        assert not dec.reset
    assert not mismatches, mismatches[:5]
    assert traded_days > 100


def _scaled(books, k):
    """The same book with every position and the equity multiplied by k (what the research's book holds when it has
    k times the money)."""
    t, c = books.trend, books.carry
    return Books(
        TrendBookState(t.equity * k, {x: q * k for x, q in t.q.items()}),
        CarryBookState(c.equity * k, {x: q * k for x, q in c.perp_q.items()}, {x: q * k for x, q in c.spot_q.items()},
                       list(c.active), list(c.last_set), dict(c.balance_price)),
        books.start_day, books.strategy)


@pytest.mark.parametrize("strategy", ["trend", "carry"])
def test_single_book_choice_with_the_whole_equity_in_the_book(doc, history, strategy):
    """The choice owns the whole equity: the book of the S3-MAJ replay scaled up to the mix's total equity
    (positions and equity both) must order the replay's orders scaled the same. Scaling changes the last bits,
    so the documented 1e-12 relative; the coins ordered must be exactly the research's."""
    from godspeed_crypto_bot.strategy import config_for

    cfg = config_for(strategy)
    mismatches = []
    for day in doc["days"]:
        d = date.fromisoformat(day["day"])
        te, ce = day["trend"]["equity"], day["carry"]["equity"]
        k = (te + ce) / (te if strategy == "trend" else ce)
        books = _books(day, None, te, ce)
        books = _scaled(books, k)
        if strategy == "trend":
            books.carry = CarryBookState(0.0)
            books.carry.equity = 0.0
        else:
            books.trend = TrendBookState(0.0)
        books.strategy = strategy
        dec = decide(history.through(date.fromordinal(d.toordinal() - 1)), books, config=cfg)
        got = dec.trend_orders if strategy == "trend" else dec.carry_orders
        want = {c: q * k for c, q in day[strategy]["orders"].items()}
        if set(got) != set(want) or not all(math.isclose(got[c], want[c], rel_tol=1e-12, abs_tol=0.0) for c in got):
            mismatches.append((day["day"], got, want))
    assert not mismatches, mismatches[:5]


@pytest.mark.parametrize("strategy", ["trend", "carry"])
def test_single_book_shadow_runs_through_the_holdout(doc, history, strategy):
    """The bot's own books, run forward in a single-book choice: the other book stays empty, the money stays
    positive, and the running cost is charged to the one book that runs."""
    from godspeed_crypto_bot.ledger import Shadow, ShadowState, slippage_for_rank
    from godspeed_crypto_bot.strategy import config_for

    h = doc["history"]
    coins = h["coins"]
    first = np.datetime64(h["first_day"], "D")

    def row(name, d):
        k = int((np.datetime64(d, "D") - first) // np.timedelta64(1, "D"))
        return {c: h[name][k][j] for j, c in enumerate(coins)}

    start_day = date.fromisoformat(doc["start_day"])
    shadow = Shadow(ShadowState(Books.fresh(2000.0, start_day, strategy)))
    for day in doc["days"]:
        d = date.fromisoformat(day["day"])
        ranks = doc["ranks"][day["day"][:7]]
        dec = decide(history.through(date.fromordinal(d.toordinal() - 1)), shadow.s.books,
                     config=config_for(strategy))
        shadow.execute(dec, row("perp_open", d), row("spot_open", d),
                       slippage=lambda c, ranks=ranks: slippage_for_rank(ranks[c]))
        shadow.settle(d, row("perp_close", d), row("spot_close", d), row("funding", d), running_per_day=5.0 / 30.0)
    b = shadow.s.books
    empty = b.carry if strategy == "trend" else b.trend
    assert empty.equity == 0.0 and not (empty.q if strategy == "carry" else empty.perp_q or empty.spot_q)
    assert (b.trend.equity if strategy == "trend" else b.carry.equity) > 0
