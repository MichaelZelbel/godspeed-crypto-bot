"""A person's own strategy file: the menu and its refusals, the plain words, the fingerprint, the engine, and the bot
running it on paper exactly like a built-in.

The proof that the engine is the built-in arithmetic: the built-in trend book written as a strategy file gives the
built-in targets on every day of the hold-out fixture (bit for bit where the fixture was made, 1e-12 elsewhere)."""

import ast
import dataclasses
import gzip
import html
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from godspeed_crypto_bot import bot, cli, config, keys, page, records, report, strategy_file
from godspeed_crypto_bot.data import build_history
from godspeed_crypto_bot.strategy import Books, CarryBookState, History, TrendBookState, config_for, decide
from godspeed_crypto_bot.strategy_file import StrategyFileError, check_bytes, explain, summary
from godspeed_crypto_bot.venue.base import Instrument
from godspeed_crypto_bot.venue.simulated import SimulatedVenue

from .conftest import INSTRUMENTS, NOW, TODAY, FakeMarket
from .test_parity import FIXTURE, _books, _equal, _same_platform

ROOT = Path(__file__).resolve().parent.parent
PERPS = {"BTC", "ETH", "SOL", "DOGE", "HYPE", "kPEPE"}

TREND_AS_FILE = b"""name = "trend-as-a-file"
description = "The built-in trend book, written as a strategy file."
coins = ["BTC", "ETH"]
rebalance = "daily"
direction = "long-short"
no_trade_band = 0.02

[signal]
kind = "momentum"
lookbacks = [20, 60, 120]

[sizing]
method = "volatility"
days = 60
target_volatility = 0.2
covariance_days = 90
gross_exposure = 2.0
max_per_coin = 1.0
"""

BASE = """name = "base"
coins = ["BTC", "ETH"]
rebalance = "daily"
direction = "long-short"

[signal]
kind = "momentum"
lookbacks = [20, 60]

[sizing]
method = "equal"
gross_exposure = 1.0
"""


def spec_of(text, perps=PERPS, **kw):
    return check_bytes(text.encode() if isinstance(text, str) else text, perps=perps, **kw)


def problems_of(text, perps=PERPS, **kw):
    with pytest.raises(StrategyFileError) as exc:
        spec_of(text, perps, **kw)
    return exc.value.problems


# ---- the hold-out proof


@pytest.fixture(scope="module")
def doc():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def history(doc):
    h = doc["history"]
    n = len(h["perp_close"])
    dates = np.arange(np.datetime64(h["first_day"], "D"), np.datetime64(h["first_day"], "D") + np.timedelta64(n, "D"))
    return History(dates, h["coins"], h["perp_open"], h["perp_high"], h["perp_low"], h["perp_close"], h["perp_traded"],
                   h["spot_open"], h["spot_high"], h["spot_low"], h["spot_close"], h["spot_traded"], h["funding"],
                   h["first_trade"], h["has_spot"])


def test_the_built_in_trend_book_as_a_file_gives_the_built_in_targets_on_every_holdout_day(doc, history):
    spec = spec_of(TREND_AS_FILE)
    exact = _same_platform(doc)
    mismatches, traded = [], 0
    for day in doc["days"]:
        d = date.fromisoformat(day["day"])
        past = history.through(date.fromordinal(d.toordinal() - 1))
        books = _books(day, None, day["trend"]["equity"], 0.0)
        books.carry = CarryBookState(0.0)
        builtin = dataclasses.replace(books)
        books.strategy = spec.book_id
        builtin.strategy = "trend"
        ours = strategy_file.decide(past, books, spec)
        theirs = decide(past, builtin, config=config_for("trend"))
        if ours.trend_orders != theirs.trend_orders or ours.trend_weights != theirs.trend_weights:
            mismatches.append((day["day"], "built-in", ours.trend_orders, theirs.trend_orders))
        if not _equal(ours.trend_orders, day["trend"]["orders"], exact):
            mismatches.append((day["day"], "fixture", ours.trend_orders, day["trend"]["orders"]))
        if ours.carry_orders or ours.reset:
            mismatches.append((day["day"], "carry or reset", ours.carry_orders))
        traded += bool(ours.trend_orders)
    assert not mismatches, mismatches[:5]
    assert traded > 100  # not vacuous: the file trades on many days
    assert len(doc["days"]) == 1016


# ---- a small recorded history, checked by hand


def small_history(n=200, funding=(0.0002, -0.0002, 0.0)):
    """BTC rises 0.5% every day, ETH falls 0.5% every day, SOL is flat. Funding per day as given (BTC longs pay, ETH
    shorts pay, SOL nothing)."""
    dates = np.arange(np.datetime64("2026-01-01", "D"), np.datetime64("2026-01-01", "D") + np.timedelta64(n, "D"))
    k = np.arange(n)[:, None]
    close = np.array([[80000.0, 2500.0, 100.0]]) * np.exp(np.array([[0.005, -0.005, 0.0]]) * k)
    open_ = np.vstack([close[:1], close[:-1]])
    traded = np.ones_like(close, dtype=bool)
    f = np.tile(np.array(funding, dtype=float), (n, 1))
    return History(dates, ("BTC", "ETH", "SOL"), open_, np.maximum(open_, close), np.minimum(open_, close), close,
                   traded, open_, np.maximum(open_, close), np.minimum(open_, close), close, traded, f,
                   [np.datetime64("2020-01-01")] * 3, [True, True, True])


def file_with(signal, coins='["BTC", "ETH", "SOL"]', sizing='method = "equal"\ngross_exposure = 1.0',
              direction="long-short", rebalance="daily", band=0.0):
    return (f'name = "t"\ncoins = {coins}\nrebalance = "{rebalance}"\ndirection = "{direction}"\n'
            f"no_trade_band = {band}\n\n[signal]\n{signal}\n\n[sizing]\n{sizing}\n")


def weights_for(text, h=None, books=None):
    h = h or small_history()
    spec = spec_of(text)
    books = books or strategy_file.fresh_books(3000.0, h.last_day + timedelta(days=1), spec.setting, spec)
    dec = strategy_file.decide(h, books, spec)
    return dec, spec


@pytest.mark.parametrize("signal,expected", [
    ('kind = "momentum"\nlookbacks = [5, 10]', {"BTC": 1 / 3, "ETH": -1 / 3}),  # SOL is flat: no vote
    ('kind = "moving-average-cross"\nfast = 5\nslow = 20', {"BTC": 1 / 3, "ETH": -1 / 3}),
    # BTC rose every day: 100 on the scale, above 70, a bet on a fall back; ETH fell every day: 0, a rise back.
    ('kind = "mean-reversion"\ndays = 14\nbuy_below = 30\nsell_above = 70', {"BTC": -1 / 3, "ETH": 1 / 3}),
    # BTC longs paid: bet on falling; ETH shorts paid: bet on rising; SOL paid nothing.
    ('kind = "funding"\ndays = 7', {"BTC": -1 / 3, "ETH": 1 / 3}),
    ('kind = "fixed"\nweights = { BTC = 0.5, ETH = -0.25, SOL = 0 }', {"BTC": 0.5, "ETH": -0.25}),
    # momentum counts three times, a fixed lean of +1 on both once: BTC (3 + 1) / 4, ETH (-3 + 1) / 4, SOL (0 + 1) / 4
    ('kind = "average"\n[[signal.parts]]\nkind = "momentum"\nlookbacks = [5, 10]\nweight = 3\n'
     '[[signal.parts]]\nkind = "fixed"\nweights = { BTC = 1, ETH = 1, SOL = 1 }\nweight = 1',
     {"BTC": 1 / 3, "ETH": -1 / 6, "SOL": 1 / 12}),
])
def test_each_signal_kind_on_a_small_recorded_history(signal, expected):
    sizing = "gross_exposure = 1.0" if signal.startswith('kind = "fixed"') else 'method = "equal"\ngross_exposure = 1.0'
    dec, spec = weights_for(file_with(signal, sizing=sizing))
    assert dec.trend_weights == pytest.approx(expected)
    h = small_history()
    for coin, w in expected.items():  # the orders are the weights at the book's equity and yesterday's close
        assert dec.trend_orders[coin] == pytest.approx(w * 3000.0 / h.perp_close[-1, h.index[coin]])
    assert not dec.carry_orders and not dec.reset
    assert dec.books_after.strategy == spec.book_id


def test_directions_keep_only_their_side():
    momentum = 'kind = "momentum"\nlookbacks = [5, 10]'
    long_only, _ = weights_for(file_with(momentum, direction="long-only"))
    short_only, _ = weights_for(file_with(momentum, direction="short-only"))
    assert long_only.trend_weights == pytest.approx({"BTC": 1 / 3})
    assert short_only.trend_weights == pytest.approx({"ETH": -1 / 3})


def test_volatility_sizing_gives_the_calmer_coin_more():
    n = 200
    dates = np.arange(np.datetime64("2026-01-01", "D"), np.datetime64("2026-01-01", "D") + np.timedelta64(n, "D"))
    k = np.arange(n)[:, None]
    # both rise; ETH swings twice as much as BTC around the same trend
    close = np.array([[100.0, 100.0]]) * np.exp(0.003 * k + np.array([[0.01, 0.02]]) * np.sin(k * 1.3))
    t = np.ones_like(close, dtype=bool)
    h = History(dates, ("BTC", "ETH"), close, close, close, close, t, close, close, close, close, t,
                np.zeros_like(close), [np.datetime64("2020-01-01")] * 2, [False, False])
    text = file_with('kind = "fixed"\nweights = { BTC = 1, ETH = 1 }', coins='["BTC", "ETH"]',
                     sizing="gross_exposure = 1.0")
    text = text.replace('kind = "fixed"\nweights = { BTC = 1, ETH = 1 }',
                        'kind = "average"\n[[signal.parts]]\nkind = "fixed"\nweights = { BTC = 1, ETH = 1 }\nweight = 1\n'
                        '[[signal.parts]]\nkind = "momentum"\nlookbacks = [5]\nweight = 1')
    text = text.replace("gross_exposure = 1.0", 'method = "volatility"\ndays = 30\ngross_exposure = 1.0')
    dec, _ = weights_for(text, h)
    w = dec.trend_weights
    assert w["BTC"] > w["ETH"] > 0
    assert w["BTC"] + w["ETH"] == pytest.approx(1.0)  # full signal on both: the whole gross exposure
    assert w["BTC"] / w["ETH"] == pytest.approx(2.0, rel=0.1)  # half the swing, about twice the share


def test_the_no_trade_band_leaves_a_close_position_alone():
    text = file_with('kind = "fixed"\nweights = { BTC = 0.5, ETH = 0, SOL = 0 }', sizing="gross_exposure = 1.0",
                     band=0.02)
    h = small_history()
    spec = spec_of(text)
    price = h.perp_close[-1, 0]
    books = Books(TrendBookState(3000.0, {"BTC": 0.49 * 3000.0 / price}), CarryBookState(0.0), date(2026, 1, 1),
                  spec.book_id)
    assert strategy_file.decide(h, books, spec).trend_orders == {}  # 49% against 50%: inside the band
    books.trend.q["BTC"] = 0.47 * 3000.0 / price
    assert strategy_file.decide(h, books, spec).trend_orders == {"BTC": pytest.approx(1500.0 / price)}


def test_weekly_and_monthly_trade_only_on_their_days():
    h = small_history()  # the decision is for the day after the last close: 2026-07-20, a Monday
    day = h.last_day + timedelta(days=1)
    assert day.weekday() == 0
    weekly = spec_of(file_with('kind = "momentum"\nlookbacks = [5]', rebalance="weekly"))
    books = Books(TrendBookState(3000.0), CarryBookState(0.0), date(2026, 1, 1), weekly.book_id)
    assert strategy_file.decide(h, books, weekly).trend_orders  # Monday
    tuesday = h.through(h.last_day - timedelta(days=6))  # a decision for Tuesday the 14th
    assert strategy_file.decide(tuesday, books, weekly).trend_orders == {}
    monthly = spec_of(file_with('kind = "momentum"\nlookbacks = [5]', rebalance="monthly"))
    books.strategy = monthly.book_id
    assert strategy_file.decide(h, books, monthly).trend_orders == {}  # the 20th
    first = h.through(date(2026, 6, 30))  # the decision for 1 July
    assert strategy_file.decide(first, books, monthly).trend_orders
    # the first day it runs, and a changed file, trade on any day
    assert strategy_file.decide(tuesday, dataclasses.replace(books, strategy="file:t#00000000"), weekly).trend_orders
    assert strategy_file.decide(tuesday, dataclasses.replace(books, start_day=date(2026, 7, 14)), weekly).trend_orders


def test_a_switch_from_the_mix_moves_the_whole_equity_and_closes_the_carry_pairs():
    h = small_history()
    spec = spec_of(file_with('kind = "momentum"\nlookbacks = [5, 10]', coins='["BTC"]'))
    books = Books(TrendBookState(1000.0, {"ETH": -0.2}), CarryBookState(1000.0, {"SOL": -5.0}, {"SOL": 5.0}, ["SOL"],
                                                                         ["SOL"], {"SOL": 100.0}),
                  date(2026, 1, 1), "mix")
    dec = strategy_file.decide(h, books, spec)
    assert (dec.trend_equity, dec.carry_equity) == (2000.0, 0.0)
    assert dec.carry_orders == {"SOL": 0.0}  # the carry pair closes
    assert dec.trend_orders["ETH"] == 0.0  # no longer in the strategy: closed
    assert dec.trend_orders["BTC"] > 0
    after = dec.books_after
    assert not after.carry.perp_q and not after.carry.spot_q and after.carry.active == []
    assert after.trend.q == {"BTC": dec.trend_orders["BTC"]} and after.strategy == spec.book_id


# ---- the limits


def test_a_file_asking_for_more_than_the_limits_is_refused():
    found = problems_of(BASE.replace("gross_exposure = 1.0", "gross_exposure = 3.0\nmax_per_coin = 1.5"))
    assert any("sizing.gross_exposure must be above 0 and at most 2" in p for p in found)
    assert any("sizing.max_per_coin must be above 0 and at most 1" in p for p in found)
    fixed = file_with('kind = "fixed"\nweights = { BTC = 1, ETH = 0.8, SOL = 0.5 }', sizing="gross_exposure = 2.0")
    assert "The fixed weights add up to 2.3 of the money, more than sizing.gross_exposure (2)." in problems_of(fixed)


def test_the_engine_never_sizes_beyond_the_limits_even_for_a_spec_that_skipped_the_check():
    h = small_history()
    spec = spec_of(file_with('kind = "momentum"\nlookbacks = [5, 10]'))
    greedy = dataclasses.replace(spec, gross_exposure=50.0, max_per_coin=10.0)
    dec = strategy_file.decide(h, strategy_file.fresh_books(3000.0, None, greedy.setting, greedy), greedy)
    assert max(abs(w) for w in dec.trend_weights.values()) <= strategy_file.MAX_PER_COIN
    assert sum(abs(w) for w in dec.trend_weights.values()) <= strategy_file.MAX_GROSS + 1e-12
    fixed = spec_of(file_with('kind = "fixed"\nweights = { BTC = 1, ETH = 1, SOL = 0 }', sizing="gross_exposure = 2.0"))
    over = dataclasses.replace(fixed, signal=dataclasses.replace(fixed.signal, weights=(("BTC", 1.0), ("ETH", -1.0),
                                                                                        ("SOL", 1.0))))
    w = strategy_file.decide(h, strategy_file.fresh_books(3000.0, None, over.setting, over), over).trend_weights
    assert sum(abs(x) for x in w.values()) == pytest.approx(2.0)


# ---- every refusal, one sentence each


REFUSALS = [
    ("name = ", "The file is not valid TOML"),
    ("leverage = 5\n" + BASE, 'Unknown setting "leverage": the settings are name, description, coins'),
    (BASE.replace('name = "base"\n', ""), "name is missing"),
    (BASE.replace('"base"', '"My Strategy"'), "name must be 1 to 40 lowercase letters, digits or dashes"),
    (BASE.replace('coins = ["BTC", "ETH"]', "coins = []"), "coins must be a list of 1 to 10 Hyperliquid perpetuals"),
    (BASE.replace('"ETH"]', '"BTC-PERP"]'), '"BTC-PERP" in coins is not a coin name'),
    (BASE.replace('"ETH"]', '"BTC"]'), "BTC is listed twice in coins."),
    (BASE.replace('"ETH"]', '"XYZ"]'), "XYZ is not a perpetual on Hyperliquid."),
    (BASE.replace('"ETH"]', '"eth"]'), "eth is not a perpetual on Hyperliquid. Did you mean ETH?"),
    (BASE.replace('"daily"', '"hourly"'), 'rebalance must be "daily", "weekly" or "monthly", not "hourly".'),
    (BASE.replace('rebalance = "daily"\n', ""), "rebalance is missing"),
    (BASE.replace('"long-short"', '"both"'), 'direction must be "long-short", "long-only" or "short-only", not "both".'),
    (BASE.replace('direction = "long-short"', 'direction = "long-short"\nno_trade_band = 0.9'),
     "no_trade_band must be a number from 0 to 0.5"),
    (BASE.replace('direction = "long-short"', 'direction = "long-short"\ndescription = 5'),
     "description must be plain text of at most 500 characters."),
    (BASE.split("[signal]")[0] + "[sizing]\nmethod = \"equal\"\n", "signal is missing"),
    (BASE.replace('"momentum"', '"breakout"'), 'signal.kind must be one of momentum, moving-average-cross'),
    (BASE.replace("lookbacks = [20, 60]", "lookbacks = [20, 60]\nthreshold = 2"),
     'Unknown setting "signal.threshold" for momentum: it takes lookbacks.'),
    (BASE.replace("lookbacks = [20, 60]", ""), "signal.lookbacks is missing: momentum needs lookbacks."),
    (BASE.replace("[20, 60]", "[0, 20]"), "signal.lookbacks must be a list of 1 to 5 different whole numbers"),
    (BASE.replace("[20, 60]", "[20, 20]"), "signal.lookbacks must be a list of 1 to 5 different whole numbers"),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "moving-average-cross"\nfast = 50\nslow = 20'),
     "signal.slow (20) must be longer than signal.fast (50)."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "moving-average-cross"\nfast = 2.5\nslow = 20'),
     "signal.fast must be a whole number of days from 2 to 365."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]',
                  'kind = "mean-reversion"\ndays = 14\nbuy_below = 60\nsell_above = 40'),
     "signal.sell_above (40) must be above signal.buy_below (60)."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "mean-reversion"\ndays = 14\nbuy_below = 30'),
     "signal.sell_above is missing: mean-reversion needs days, buy_below and sell_above."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "funding"\ndays = 90'),
     "signal.days must be a whole number of days from 1 to 30."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "funding"\ndays = 7\nmin_annual_rate = -1'),
     "signal.min_annual_rate must be a number from 0 to 5"),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "fixed"\nweights = { BTC = 0.5 }')
     .replace('method = "equal"\n', ""), "signal.weights has no weight for ETH."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "fixed"\nweights = { BTC = 0.5, ETH = 0.1, SOL = 0.1 }')
     .replace('method = "equal"\n', ""), 'signal.weights names "SOL", which is not in coins.'),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "fixed"\nweights = { BTC = 2, ETH = 0 }')
     .replace('method = "equal"\n', ""), "signal.weights.BTC must be a number from -1 to 1"),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "fixed"\nweights = { BTC = 0.5, ETH = -0.2 }')
     .replace('method = "equal"\n', "").replace('"long-short"', '"long-only"'),
     'signal.weights.ETH is negative, a bet on falling, but direction is "long-only".'),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]', 'kind = "fixed"\nweights = { BTC = 0.5, ETH = 0.2 }'),
     "A fixed strategy sets its own sizes in signal.weights, so sizing.method does not apply; leave it out."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]',
                  'kind = "average"\n[[signal.parts]]\nkind = "momentum"\nlookbacks = [5]\nweight = 1'),
     "signal.parts must be a list of 2 to 5 signals"),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]',
                  'kind = "average"\n[[signal.parts]]\nkind = "momentum"\nlookbacks = [5]\nweight = 0\n'
                  '[[signal.parts]]\nkind = "funding"\ndays = 7\nweight = 1'),
     "signal.parts[1].weight must be a number above 0 and at most 10."),
    (BASE.replace('kind = "momentum"\nlookbacks = [20, 60]',
                  'kind = "average"\n[[signal.parts]]\nkind = "average"\nweight = 1\n'
                  '[[signal.parts]]\nkind = "funding"\ndays = 7\nweight = 1'),
     'signal.parts[1].kind cannot be "average" inside an average.'),
    (BASE.split("[sizing]")[0], 'sizing is missing: add a [sizing] table with method = "equal" or "volatility".'),
    (BASE.replace('method = "equal"', 'method = "kelly"'), 'sizing.method must be "equal" or "volatility", not "kelly".'),
    (BASE.replace('method = "equal"', 'method = "volatility"'), "sizing.days is missing"),
    (BASE.replace('method = "equal"', 'method = "volatility"\ndays = 5'),
     "sizing.days must be a whole number of days from 10 to 365."),
    (BASE.replace('method = "equal"', 'method = "volatility"\ndays = 60\ncovariance_days = 90'),
     "sizing.covariance_days only applies together with sizing.target_volatility."),
    (BASE.replace('method = "equal"', 'method = "volatility"\ndays = 60\ntarget_volatility = 3'),
     "sizing.target_volatility must be a number above 0 and at most 1"),
    (BASE.replace('method = "equal"', 'method = "equal"\nleverage = 3'),
     'Unknown setting "sizing.leverage" for equal sizing: it takes method, gross_exposure and max_per_coin.'),
    (BASE.replace('method = "equal"', 'method = "equal"\ndays = 30'),
     'Unknown setting "sizing.days" for equal sizing'),
]


@pytest.mark.parametrize("case", REFUSALS, ids=lambda c: c[-1][:50])
def test_every_refusal_gives_its_sentence(case):
    text, sentence = case[0], case[-1]
    found = problems_of(text)
    assert any(sentence in p for p in found), found
    for p in found:  # one plain sentence each, nothing echoed raw
        assert p.endswith((".", "?")) and "\n" not in p and "\u2014" not in p


def test_the_name_inside_must_match_the_files_name(tmp_path):
    p = tmp_path / "other.toml"
    p.write_text(BASE, encoding="utf-8")
    with pytest.raises(StrategyFileError) as exc:
        strategy_file.load(p, perps=PERPS)
    assert exc.value.problems == ["The name inside the file (base) differs from the file's name (other.toml); make "
                                  "them the same."]


def test_all_problems_are_listed_at_once_and_a_value_is_quoted_safely():
    text = BASE.replace('"daily"', '"x\\u0007y"').replace('"long-short"', '"sideways"') + "extra = 1\n"
    found = problems_of(text)
    assert len(found) == 3
    assert any('not "x?y".' in p for p in found)  # a control character is never echoed


def test_a_large_or_binary_file_is_refused(tmp_path):
    assert problems_of(b"#" * (strategy_file.MAX_FILE_BYTES + 1))[0].startswith("The file is larger than 16 KB")
    assert problems_of(b"\xff\xfe\x00") == ["The file is not plain text (UTF-8)."]
    with pytest.raises(StrategyFileError) as exc:
        strategy_file.load(tmp_path / "missing.toml")
    assert "There is no strategy file missing.toml" in exc.value.problems[0]


def test_without_the_exchange_list_the_coins_are_not_checked_against_it():
    assert spec_of(BASE.replace('"ETH"]', '"NEWCOIN"]'), perps=None).coins == ("BTC", "NEWCOIN")


# ---- a strategy file is data


def test_nothing_in_a_strategy_file_can_run_code():
    """The module that reads strategy files has no way to run anything: no eval, exec, compile, import machinery,
    pickle, shell or process call, and it reads TOML only."""
    tree = ast.parse((ROOT / "godspeed_crypto_bot" / "strategy_file.py").read_text(encoding="utf-8"))
    banned_names = {"eval", "exec", "compile", "__import__", "setattr", "globals", "locals", "vars", "open_code"}
    banned_modules = {"subprocess", "importlib", "pickle", "marshal", "shlex", "runpy", "ctypes", "yaml"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned_names, node.func.id
            if node.func.id == "getattr":  # only fixed names, never one read from a file
                assert isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert node.attr in {"open", "O_RDONLY", "O_NOFOLLOW", "O_NONBLOCK", "fdopen", "fstat"}, node.attr
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not banned_modules & {m.split(".")[0] for m in mods}, mods
    hostile = BASE.replace('name = "base"', 'name = "base"\ndescription = "__import__(\'os\').system(\'touch x\')"')
    assert spec_of(hostile).description.startswith("__import__")  # kept as words, never run
    assert "__import__" not in explain(spec_of(hostile))


# ---- plain words and the fingerprint


def test_the_plain_words_say_what_was_understood():
    weekly = spec_of(TREND_AS_FILE.replace(b'"daily"', b'"weekly"'))
    text = explain(weekly)
    assert text.startswith("Every Monday, it looks at how BTC and ETH moved over the last 20, 60 and 120 days")
    assert "It bets on rises and on falls." in text
    assert "about 20% a year" in text and "at most 100% of the money in one coin and 200% in all" in text
    assert "within 2% of the money of its target" in text
    assert summary(weekly) == "follows the moves over 20, 60 and 120 days on BTC and ETH, every Monday"
    fixed = spec_of(file_with('kind = "fixed"\nweights = { BTC = 0.6, ETH = 0.4, SOL = 0 }', rebalance="monthly",
                              sizing="gross_exposure = 1.0"))
    assert explain(fixed).startswith("On the first day of every month, it sets back 60% of the money as a bet on BTC "
                                     "rising, 40% of the money as a bet on ETH rising and nothing in SOL.")


@pytest.mark.parametrize("signal", [
    'kind = "moving-average-cross"\nfast = 20\nslow = 100',
    'kind = "mean-reversion"\ndays = 14\nbuy_below = 30\nsell_above = 70',
    'kind = "funding"\ndays = 7\nmin_annual_rate = 0.1',
    'kind = "average"\n[[signal.parts]]\nkind = "momentum"\nlookbacks = [30]\nweight = 2\n'
    '[[signal.parts]]\nkind = "fixed"\nweights = { BTC = 1, ETH = -0.5, SOL = 0 }\nweight = 1',
])
def test_every_kind_has_plain_words_without_forecasts(signal):
    text = explain(spec_of(file_with(signal, direction="long-only")))
    assert "It only bets on rises" in text and "\u2014" not in text
    assert not re.search(r"\b(profit|return|gain|earn|guarantee|forecast|safe)\w*", text, re.I), text


def test_the_fingerprint_names_the_file():
    a = spec_of(BASE).fingerprint
    assert re.fullmatch(r"[0-9a-f]{8}", a)
    assert spec_of(BASE.replace("\n", "\r\n")).fingerprint == a  # the same file through a Windows editor
    assert spec_of(BASE + "# a comment\n").fingerprint != a  # any change is a new fingerprint
    assert spec_of(BASE).book_id == f"file:base#{a}"


def test_every_example_in_the_documentation_passes_the_check():
    text = (ROOT / "docs" / "strategy-files.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```toml\n(.*?)```", text, re.S)
    assert len(blocks) >= 3
    for block in blocks:
        spec = spec_of(block)
        assert explain(spec)
    assert "\u2014" not in text


# ---- the setting


def test_the_setting_takes_a_file_name_and_refuses_anything_else(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('strategy = "file:my-trend"\n')
    assert config.load(p).strategy == "file:my-trend"
    for bad in ("file:", "file:My Trend", "file:../etc/passwd", "file:a/b"):
        p.write_text(f'strategy = "{bad}"\n')
        with pytest.raises(ValueError) as exc:
            config.load(p)
        assert 'strategy must be "mix", "trend" or "carry", or "file:<name>"' in str(exc.value)
    assert config.Config().strategies_dir == "/etc/godspeed-crypto-bot/strategies"


# ---- the bot runs it on paper like a built-in


class DogeMarket(FakeMarket):
    """The test market with one more perpetual, DOGE, that no built-in strategy trades."""

    def __init__(self, today=TODAY, **kw):
        super().__init__(today=today, **kw)
        self.funding_hourly = {**self.funding_hourly, "DOGE": 0.000002}
        p = self.close_on("DOGE", today - timedelta(days=1))
        self.mid["DOGE"] = self.marks["DOGE"] = p

    def close_on(self, coin, d):
        if coin == "DOGE":
            k = (d - (self.today - timedelta(days=400))).days
            return 0.2 * math.exp(0.002 * k + 0.03 * math.sin(0.7 * k))
        return super().close_on(coin, d)

    def instruments(self):
        return {**INSTRUMENTS, ("DOGE", "perp"): Instrument("DOGE", "perp", "DOGE", 0, 6, None, 10)}


def at(day, h=0, m=2):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def own(cfg, tmp_path, text=None, name="momo"):
    folder = tmp_path / "strategies"
    folder.mkdir(exist_ok=True)
    text = text or file_with('kind = "momentum"\nlookbacks = [20, 60]', coins='["BTC", "DOGE"]').replace(
        'name = "t"', f'name = "{name}"')
    (folder / f"{name}.toml").write_text(text, encoding="utf-8")
    cfg.strategies_dir = str(folder)
    cfg.strategy = f"file:{name}"
    cfg.mode = "paper"
    return cfg


@pytest.fixture
def paper_store(cfg):
    s = records.Store(cfg.db_path, "paper", "mainnet")
    yield s
    s.close()


def test_the_daily_run_trades_a_strategy_file_on_paper_and_names_it_everywhere(cfg, tmp_path, paper_store, notifier):
    own(cfg, tmp_path)
    market = DogeMarket()
    venue = SimulatedVenue(market, paper_store, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())
    out = bot.run_daily(cfg, venue, paper_store, notifier, now=at(TODAY))
    assert out["status"] == "complete"
    acct = venue.account()
    assert acct.perp_size("BTC") > 0 and acct.perp_size("DOGE") > 0  # both rise in the test market
    assert acct.perp_size("ETH") == 0 and not any(q for t, (q, _h) in acct.spot.items() if t != "USDC")
    assert set(out["targets"]) >= {"BTC", "DOGE"}
    snap = paper_store.get("expected_holdings")["coins"]
    assert snap["DOGE"]["perp"] == acct.perp_size("DOGE")  # the guard watches the file's coins too
    assert bot.coins_for(cfg, paper_store) == ("BTC", "ETH", "SOL", "DOGE")
    words = 'your own strategy "momo": follows the moves over 20 and 60 days on BTC and DOGE, every day'
    assert bot.status(cfg, paper_store)["strategy_in_words"] == words
    tr = page.Track(cfg, "main")
    assert html.escape(words) in page.headline(tr)
    paragraph = page.explain(cfg.strategy, cfg)
    assert '<b>Your own strategy, "momo".</b> Every day, it looks at how BTC and DOGE moved' in paragraph
    _path, text = report.weekly(cfg, paper_store, today=TODAY, out_dir=tmp_path / "reports")
    assert words in text
    again = bot.run_daily(cfg, venue, paper_store, notifier, now=at(TODAY))
    assert "nothing traded again" in again["note"]


def test_switching_a_running_bot_from_the_mix_to_a_file_closes_the_carry_trade(cfg, tmp_path, paper_store, notifier):
    cfg.mode = "paper"
    market = DogeMarket()
    venue = SimulatedVenue(market, paper_store, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())
    bot.run_daily(cfg, venue, paper_store, notifier, now=at(TODAY))
    assert venue.account().spot_size("USOL") > 0  # the mix holds the SOL carry pair
    own(cfg, tmp_path, file_with('kind = "momentum"\nlookbacks = [20, 60]', coins='["DOGE"]').replace(
        'name = "t"', 'name = "momo"'))
    tomorrow = TODAY + timedelta(days=1)
    m2 = DogeMarket(today=tomorrow)
    v2 = SimulatedVenue(m2, paper_store, "book", start_usdc=2000.0, now=lambda: at(tomorrow).timestamp())
    out = bot.run_daily(cfg, v2, paper_store, notifier, now=at(tomorrow))
    assert out["status"] == "complete"
    acct = v2.account()
    assert acct.spot_size("USOL") == 0 and acct.perp_size("SOL") == 0  # the carry pair is closed
    assert acct.perp_size("BTC") == 0 and acct.perp_size("ETH") == 0  # the trend half too
    assert acct.perp_size("DOGE") > 0
    books = paper_store.books_on(tomorrow)["books"]
    assert books["strategy"].startswith("file:momo#") and not books["carry"]["perp_q"] and not books["carry"]["spot_q"]
    assert -20.0 < books["carry"]["equity"] <= 0.0  # what closing the pair cost, booked to the book it closed
    day3 = tomorrow + timedelta(days=1)
    m3 = DogeMarket(today=day3)
    v3 = SimulatedVenue(m3, paper_store, "book", start_usdc=2000.0, now=lambda: at(day3).timestamp())
    assert bot.run_daily(cfg, v3, paper_store, notifier, now=at(day3))["status"] == "complete"
    books = paper_store.books_on(day3)["books"]
    assert books["carry"]["equity"] == 0.0 and books["trend"]["equity"] > 1900.0  # it joined the file strategy


def test_a_weekly_file_leaves_the_account_alone_between_its_days(cfg, tmp_path, paper_store, notifier):
    own(cfg, tmp_path, file_with('kind = "momentum"\nlookbacks = [20, 60]', coins='["BTC"]', rebalance="weekly")
        .replace('name = "t"', 'name = "momo"'))
    assert TODAY.weekday() == 1  # a Tuesday: the first day trades anyway
    market = DogeMarket()
    venue = SimulatedVenue(market, paper_store, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())
    bot.run_daily(cfg, venue, paper_store, notifier, now=at(TODAY))
    held = venue.account().perp_size("BTC")
    assert held > 0
    tomorrow = TODAY + timedelta(days=1)
    m2 = DogeMarket(today=tomorrow)
    v2 = SimulatedVenue(m2, paper_store, "book", start_usdc=2000.0, now=lambda: at(tomorrow).timestamp())
    v2.st["perp_cash"] += 500.0  # the account grew: a daily strategy would buy more today
    v2._save()
    out = bot.run_daily(cfg, v2, paper_store, notifier, now=at(tomorrow))
    assert out["status"] == "complete" and out["trend_orders"] == {}
    assert out["orders"] == [] and v2.account().perp_size("BTC") == held
    assert any("not a weekly trading day for momo" in n for n in out["restrictions"])


def test_a_broken_file_stops_the_daily_run_with_its_sentence(cfg, tmp_path, monkeypatch, capsys):
    own(cfg, tmp_path, BASE.replace('name = "base"', 'name = "momo"').replace('"daily"', '"hourly"'))
    p = tmp_path / "c.toml"
    p.write_text(f'mode = "paper"\nstrategy = "file:momo"\nstrategies_dir = "{tmp_path.as_posix()}/strategies"\n'
                 f'state_dir = "{tmp_path.as_posix()}/state"\n')
    monkeypatch.setattr(bot, "build_venue", lambda c, s, **kw: SimulatedVenue(DogeMarket(), s, "book", 2000.0,
                                                                              now=lambda: NOW.timestamp()))
    monkeypatch.setattr(bot, "utcnow", lambda: at(TODAY))
    code = cli.main(["--config", str(p), "run-daily"])
    err = capsys.readouterr().err
    assert code == cli.EXIT_CONFIG
    assert 'rebalance must be "daily", "weekly" or "monthly", not "hourly".' in err
    s = records.Store(tmp_path / "state" / "bot.sqlite", "paper", "mainnet")
    try:
        assert s.run(TODAY)["status"] == "failed" and "hourly" in s.run(TODAY)["error"]
        status = bot.status(config.load(p), s)
        assert status["strategy_in_words"].startswith('your own strategy "momo" (its file cannot be used: rebalance')
    finally:
        s.close()


class QuietDogeMarket(DogeMarket):
    """DOGE has not traded yet today, and had no trade at all three days ago."""

    def daily_candles(self, inst, start, end):
        out = super().daily_candles(inst, start, end)
        if inst.coin == "DOGE":
            out = [c for c in out if c.day not in (self.today, self.today - timedelta(days=3))]
        return out


def test_a_quiet_coin_without_a_candle_neither_stops_the_run_nor_the_shadow_ledger(cfg, tmp_path, paper_store,
                                                                                  notifier):
    own(cfg, tmp_path)
    m = QuietDogeMarket()
    h, snap = build_history(m, TODAY, ("BTC", "ETH", "SOL", "DOGE"), 400, required=("BTC", "ETH", "SOL"))
    yesterday = TODAY - timedelta(days=1)
    assert snap.open_perp["DOGE"] == snap.close_perp["DOGE"][yesterday]  # its last close stands in for the open
    assert not np.isfinite(h.perp_close[-3, h.index["DOGE"]])  # the quiet day has no valid price
    venue = SimulatedVenue(m, paper_store, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())
    assert bot.run_daily(cfg, venue, paper_store, notifier, now=at(TODAY))["status"] == "complete"
    # three days later the ledger books the missed days, the quiet one at DOGE's last close
    later = TODAY + timedelta(days=3)
    m2 = QuietDogeMarket(today=later)
    m2_candles = m2.daily_candles
    m2.daily_candles = lambda inst, s, e: [c for c in m2_candles(inst, s, e)
                                           if not (inst.coin == "DOGE" and c.day == TODAY + timedelta(days=1))]
    v2 = SimulatedVenue(m2, paper_store, "book", start_usdc=2000.0, now=lambda: at(later).timestamp())
    assert bot.run_daily(cfg, v2, paper_store, notifier, now=at(later))["status"] == "complete"


def test_a_coin_the_exchange_no_longer_lists_is_left_out_without_stopping_the_run():
    m = FakeMarket()
    h, snap = build_history(m, TODAY, ("BTC", "ETH", "SOL", "GONE"), 400, required=("BTC", "ETH", "SOL"))
    assert h.coins == ("BTC", "ETH", "SOL")
    assert "GONE perp is not listed on mainnet; left out" in snap.warnings


class DelistedDogeMarket(FakeMarket):
    """The day after: Hyperliquid no longer lists DOGE (no instrument, no price)."""


def test_a_held_coin_the_exchange_delists_neither_crashes_the_run_nor_the_guard(cfg, tmp_path, paper_store, notifier):
    own(cfg, tmp_path)
    venue = SimulatedVenue(DogeMarket(), paper_store, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())
    bot.run_daily(cfg, venue, paper_store, notifier, now=at(TODAY))
    held = venue.account().perp_size("DOGE")
    assert held > 0
    later = TODAY + timedelta(days=2)
    gone = DelistedDogeMarket(today=later)
    v2 = SimulatedVenue(gone, paper_store, "book", start_usdc=2000.0, now=lambda: at(later).timestamp())
    acct = v2.account()  # the paper account holds it at its entry price until the exchange would settle it
    assert acct.perp_size("DOGE") == held and acct.positions["DOGE"].unrealized_pnl == 0.0
    out = bot.run_daily(cfg, v2, paper_store, notifier, now=at(later))
    assert out["status"] == "complete"
    assert any("DOGE perp is not listed" in w for w in out["warnings"])
    books = paper_store.books_on(later)["books"]
    assert books["trend"]["q"]["DOGE"] == pytest.approx(held, rel=0.2)  # kept in the books at its last mark
    assert bot.guard(cfg, v2, paper_store, notifier, now=at(later, 3))["holdings"]["status"] in ("ok", "rebaselined")


# ---- the commands


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(cli, "_listed_perps", lambda cfg: set(PERPS))


def write_config(tmp_path, extra=""):
    p = tmp_path / "c.toml"
    p.write_text(f'mode = "paper"\nstrategies_dir = "{tmp_path.as_posix()}/strategies"\n'
                 f'state_dir = "{tmp_path.as_posix()}/state"\n{extra}', encoding="utf-8")
    (tmp_path / "strategies").mkdir(exist_ok=True)
    return p


def test_strategy_check_says_what_it_understood_or_every_problem(tmp_path, listed, capsys):
    p = write_config(tmp_path)
    good = tmp_path / "trend-as-a-file.toml"
    good.write_bytes(TREND_AS_FILE)
    assert cli.main(["--config", str(p), "strategy", "check", str(good)]) == 0
    out = capsys.readouterr().out
    spec = spec_of(TREND_AS_FILE)
    assert out.startswith('Strategy "trend-as-a-file", as the bot understood it:\nEvery day, it looks at how BTC')
    assert f"Fingerprint: {spec.fingerprint}" in out
    assert "configure --strategy file:trend-as-a-file" in out
    bad = tmp_path / "base.toml"
    bad.write_text(BASE.replace('"daily"', '"hourly"').replace('"ETH"]', '"XYZ"]'), encoding="utf-8")
    assert cli.main(["--config", str(p), "strategy", "check", str(bad)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "The bot cannot use this strategy file:"
    assert "- XYZ is not a perpetual on Hyperliquid." in out
    assert '- rebalance must be "daily", "weekly" or "monthly", not "hourly".' in out
    assert cli.main(["--config", str(p), "strategy", "check", str(good), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] and doc["fingerprint"] == spec.fingerprint and doc["setting"] == "file:trend-as-a-file"


def test_strategy_check_refuses_when_the_coins_cannot_be_checked(tmp_path, monkeypatch, capsys):
    def offline(cfg):
        raise ConnectionError("no network")

    monkeypatch.setattr(cli, "_listed_perps", offline)
    p = write_config(tmp_path)
    f = tmp_path / "base.toml"
    f.write_text(BASE, encoding="utf-8")
    assert cli.main(["--config", str(p), "strategy", "check", str(f)]) == 1
    assert "Could not reach Hyperliquid to check that the coins exist" in capsys.readouterr().out


def test_configure_checks_the_file_first_and_refuses_a_failing_one(tmp_path, listed, capsys):
    p = write_config(tmp_path)
    before = p.read_text()
    (tmp_path / "strategies" / "base.toml").write_text(BASE.replace('"ETH"]', '"XYZ"]'), encoding="utf-8")
    assert cli.main(["--config", str(p), "configure", "--strategy", "file:base"]) == 1
    err = capsys.readouterr().err
    assert "cannot be used, so nothing was changed" in err and "- XYZ is not a perpetual on Hyperliquid." in err
    assert p.read_text() == before
    assert cli.main(["--config", str(p), "configure", "--strategy", "file:missing"]) == 1
    assert "There is no strategy file missing.toml" in capsys.readouterr().err
    (tmp_path / "strategies" / "base.toml").write_text(BASE, encoding="utf-8")
    assert cli.main(["--config", str(p), "configure", "--strategy", "file:base"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["strategy"] == "file:base" and doc["strategy_in_words"].startswith('your own strategy "base"')
    assert config.load(p).strategy == "file:base"
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(p), "configure", "--strategy", "file:Not Valid"])
    assert exc.value.code == 2


def test_configure_checks_the_file_on_the_network_it_switches_to(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_listed_perps", lambda cfg: {"mainnet": {"BTC", "ETH", "BCH"},
                                                           "testnet": {"BTC", "ETH"}}[cfg.network])
    p = write_config(tmp_path)
    (tmp_path / "strategies" / "base.toml").write_text(BASE.replace('"ETH"]', '"BCH"]'), encoding="utf-8")
    assert cli.main(["--config", str(p), "configure", "--network", "testnet", "--strategy", "file:base"]) == 1
    assert "- BCH is not a perpetual on Hyperliquid." in capsys.readouterr().err
    assert config.load(p).network == "mainnet" and config.load(p).strategy == "mix"
    assert cli.main(["--config", str(p), "configure", "--strategy", "file:base"]) == 0  # fine on mainnet
    capsys.readouterr()
    assert cli.main(["--config", str(p), "configure", "--network", "testnet"]) == 1  # the file it runs is checked too
    assert "- BCH is not a perpetual on Hyperliquid." in capsys.readouterr().err
    assert config.load(p).network == "mainnet"


def test_a_strategy_file_stays_on_paper_in_this_version(tmp_path, listed, cfg, capsys):
    p = write_config(tmp_path, 'mode = "live"\n'.replace("mode", "# mode"))
    text = p.read_text().replace('mode = "paper"', 'mode = "live"')
    p.write_text(text)
    (tmp_path / "strategies" / "base.toml").write_text(BASE, encoding="utf-8")
    assert cli.main(["--config", str(p), "configure", "--strategy", "file:base"]) == 1
    assert "a strategy file runs on paper and in dry-run only in this version" in capsys.readouterr().err
    assert config.load(p).strategy == "mix"
    cfg.mode, cfg.strategy = "live", "file:base"
    assert keys.FILE_NOT_LIVE in keys.live_problems(cfg)
    cfg.strategy = "trend"
    assert keys.FILE_NOT_LIVE not in keys.live_problems(cfg)
