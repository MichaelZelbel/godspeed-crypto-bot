"""`godspeed-crypto-bot backtest`: a strategy tested against Hyperliquid's past prices before it trades.

It reads only public market data (daily candles and hourly funding, through venue/hyperliquid.py), never signs and
needs no key, so it runs in any mode. What it fetches is cached in the bot's state folder (backtests/cache/) and only
what is missing is fetched again. It runs the strategy day by day with the shadow ledger itself (ledger.py): orders
fill at the day's open with the ledger's fee and slippage tiers, positions are marked at each close and pay or receive
that day's funding. So the test and the bot's own paper record are the same arithmetic on the same days.

The report (Markdown and JSON, in backtests/) shows the period and why it starts where it does, the worst day, week
and month, the three worst weeks, the deepest fall from a high point and how long it lasted, the fees, slippage and
funding, how often it traded, the result over the whole period and the same for holding an equal share of the coins.
Its first line says it is a test against the past, not a forecast. It tests exactly the strategy it is given: there is
no search over settings. A report is written only when the whole test ran; a failure midway leaves none.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import strategy_file
from .data import DAY_MS, day_ms, funding_through_close
from .ledger import DEFAULT_SLIPPAGE, MODEL_SLIPPAGE, PERP_FEE, SPOT_FEE, Shadow, ShadowState
from .names import DISPLAY_NAME
from .strategy import MIN_LISTING_DAYS, STRATEGIES, STRATEGY_WORDS, History, config_for, decide

FIRST_LINE = ("This is a test against the past, not a forecast: it shows what these rules would have done on "
              "Hyperliquid's past prices, and what comes next can be very different.")
BUILTIN_COINS = {"trend": ("BTC", "ETH"), "carry": ("BTC", "ETH", "SOL"), "mix": ("BTC", "ETH", "SOL")}
EARLIEST = date(2020, 1, 1)  # asking for candles from here returns everything Hyperliquid has
CACHE_FORMAT = 1


class BacktestError(RuntimeError):
    """One plain sentence for the person; no report was written."""


# ---- the prices, cached


def _iso(d):
    return d.isoformat()


def _write_json(path: Path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("format") == CACHE_FORMAT else None


def _patient(fn, *args, attempts=6, sleep=time.sleep):
    """Call Hyperliquid, waiting and trying again when it says too many requests (HTTP 429)."""
    delay = 2.0
    for k in range(attempts):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - only "too many requests" is waited out
            if getattr(exc, "status_code", None) != 429 or k == attempts - 1:
                raise
            sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


class PriceCache:
    """Daily candles per instrument and daily funding per coin from Hyperliquid, kept in ``folder``.

    Only closed days are kept. A later test fetches only the days after the last one kept (candles) or outside the
    range already kept (funding)."""

    def __init__(self, folder, market, last_day: date, sleep=time.sleep):
        self.folder = Path(folder)
        self.market, self.last_day, self.sleep = market, last_day, sleep

    def candles(self, inst):
        """{day: (open, high, low, close, trades)} for every closed day Hyperliquid has, through ``last_day``."""
        path = self.folder / f"{inst.market}-{inst.coin}.json"
        doc = _read_json(path) or {"format": CACHE_FORMAT, "name": inst.name, "candles": {}, "through": None}
        if doc.get("name") != inst.name:  # the exchange renamed the market: start again
            doc = {"format": CACHE_FORMAT, "name": inst.name, "candles": {}, "through": None}
        through = date.fromisoformat(doc["through"]) if doc["through"] else None
        if through is None or through < self.last_day:
            start = EARLIEST if through is None else through + timedelta(days=1)
            rows = _patient(self.market.daily_candles, inst, start, self.last_day, sleep=self.sleep)
            for c in rows:
                if c.closed and c.day <= self.last_day:
                    doc["candles"][_iso(c.day)] = [c.open, c.high, c.low, c.close, c.trades]
            doc["through"] = _iso(self.last_day)
            _write_json(path, doc)
        return {date.fromisoformat(k): tuple(v) for k, v in doc["candles"].items()}

    def funding(self, coin, first: date, last: date):
        """{day: (rate sum, settlements)} for first..last, the way the bot sums funding per day."""
        path = self.folder / f"funding-{coin}.json"
        doc = _read_json(path) or {"format": CACHE_FORMAT, "days": {}, "from": None, "through": None}
        have_from = date.fromisoformat(doc["from"]) if doc["from"] else None
        have_through = date.fromisoformat(doc["through"]) if doc["through"] else None
        spans = []
        if have_from is None:
            spans.append((first, last))
        else:
            if first < have_from:
                spans.append((first, have_from - timedelta(days=1)))
            if last > have_through:
                spans.append((have_through + timedelta(days=1), last))
        for a, b in spans:
            events = _patient(self.market.funding_history, coin, day_ms(a) - DAY_MS, day_ms(b) + DAY_MS + 60_000,
                              sleep=self.sleep)
            for d, (s, n) in funding_through_close(events, a, b).items():
                doc["days"][_iso(d)] = [s, n]
        if spans:
            doc["from"] = _iso(min(first, have_from) if have_from else first)
            doc["through"] = _iso(max(last, have_through) if have_through else last)
            _write_json(path, doc)
        out = {}
        for k, v in doc["days"].items():
            d = date.fromisoformat(k)
            if first <= d <= last:
                out[d] = (float(v[0]), int(v[1]))
        return out


# ---- the period and the history


@dataclass
class Market:
    """What a test reads: the history through its last day, its period and why, and what the data lacked."""

    history: History
    first_traded: dict  # coin -> first day Hyperliquid has trades for it
    start: date
    end: date
    why: str
    spot_first: dict = field(default_factory=dict)  # carry coin -> first day its spot token traded
    missing_funding: dict = field(default_factory=dict)  # coin -> days of the test without funding records


def strategy_parts(cfg, setting):
    """(label, words, coins, spot coins, spec) for a built-in name or file:<name>."""
    if setting in STRATEGIES:
        coins = BUILTIN_COINS[setting]
        return setting, STRATEGY_WORDS[setting], coins, (coins if setting != "trend" else ()), None
    if not strategy_file.is_file_setting(setting):
        raise BacktestError(f'Say which strategy to test: "mix", "trend", "carry" or "file:<name>", not {setting!r}.')
    try:
        spec = strategy_file.load(strategy_file.path_for(cfg.strategies_dir, strategy_file.setting_name(setting)))
    except strategy_file.StrategyFileError as exc:
        raise BacktestError("The strategy file cannot be used: " + " ".join(exc.problems)) from None
    return (f"file-{spec.name}-{spec.fingerprint}", f'your own strategy "{spec.name}": {strategy_file.summary(spec)}',
            spec.coins, (), spec)


def needed_days(spec):
    """Days of prices every coin must have before the first day of the test, so the strategy can trade it then."""
    if spec is None:
        return MIN_LISTING_DAYS + 1  # the trend book's longest look-back, 120 days, plus the day itself
    return spec.history_days() - 30 + 1


def funding_window(spec):
    """Days of funding before the first day that the signal reads (the carry book's seven)."""
    return 7 if spec is None else max(1, spec.funding_days() - 2)


def period(first_traded, spec, last_day, start=None, end=None):
    """(start, end, why): the first day every coin has the days of prices the strategy needs, unless a later start
    was asked for, through the last closed day or the end asked for."""
    need = needed_days(spec)
    latest = max(first_traded, key=lambda c: (first_traded[c], c))
    earliest = first_traded[latest] + timedelta(days=need)
    end = min(end, last_day) if end else last_day
    names = strategy_file._list_words(list(first_traded))
    youngest = [c for c in first_traded if first_traded[c] == first_traded[latest]]
    every = {1: "", 2: " both"}.get(len(first_traded), " all")
    why = (f"It starts on {earliest}, the first day on which {names}{every} had the {need} days of daily prices on "
           "Hyperliquid that the strategy needs before it trades; ")
    if len(youngest) == len(first_traded):
        why += f"{'their' if len(youngest) > 1 else 'its'} daily prices there begin on {first_traded[latest]}."
    else:
        why += (f"{strategy_file._list_words(youngest)} {'has' if len(youngest) == 1 else 'have'} the shortest "
                f"history there, from {first_traded[latest]}.")
    if start and start > earliest:
        why = f"It starts on {start}, as asked. " + why.replace("It starts on", "The earliest possible start is", 1)
        earliest = start
    elif start and start < earliest:
        why = f"You asked for {start}, which is too early. " + why
    if earliest > end:
        raise BacktestError(f"There is not enough history to test this: the earliest possible start is {earliest}, "
                            f"after the last day to test ({end}).")
    return earliest, end, why


def load_market(cache, market, coins, spot_coins, spec, last_day, start=None, end=None) -> Market:
    """Every coin's daily candles, the carry coins' spot candles, the period, and the funding the test needs: a
    History from the first traded day of any coin through the test's last day."""
    insts = market.instruments()
    perp, spot, first_traded, spot_first = {}, {}, {}, {}
    for coin in coins:
        inst = insts.get((coin, "perp"))
        if inst is None:
            raise BacktestError(f"{coin} is not a perpetual on Hyperliquid now, so it cannot be tested.")
        perp[coin] = cache.candles(inst)
        traded = sorted(d for d, c in perp[coin].items() if c[4] > 0)
        if not traded:
            raise BacktestError(f"Hyperliquid has no traded daily prices for {coin} yet.")
        first_traded[coin] = traded[0]
    for coin in spot_coins:
        inst = insts.get((coin, "spot"))
        if inst is not None:
            spot[coin] = cache.candles(inst)
            traded = sorted(d for d, c in spot[coin].items() if c[4] > 0)
            if traded:
                spot_first[coin] = traded[0]
    start, end, why = period(first_traded, spec, last_day, start, end)
    funding, missing = {}, {}
    window = funding_window(spec) + 1
    for coin in coins:
        funding[coin] = cache.funding(coin, max(start - timedelta(days=window), first_traded[coin]), end)
        gaps = sum(1 for k in range((end - start).days + 1) if not funding[coin].get(start + timedelta(days=k),
                                                                                    (0.0, 0))[1])
        if gaps:
            missing[coin] = gaps
    first = min(first_traded.values())
    days = np.arange(np.datetime64(first, "D"), np.datetime64(end, "D") + np.timedelta64(1, "D"))
    shape = (len(days), len(coins))
    arr = {k: np.full(shape, np.nan) for k in ("po", "ph", "pl", "pc", "so", "sh", "sl", "sc", "f")}
    ptr, stra = np.zeros(shape, dtype=bool), np.zeros(shape, dtype=bool)
    for j, coin in enumerate(coins):
        for d, (o, h, lo, c, n) in perp[coin].items():
            k = (d - first).days
            if 0 <= k < len(days):
                arr["po"][k, j], arr["ph"][k, j], arr["pl"][k, j], arr["pc"][k, j] = o, h, lo, c
                ptr[k, j] = n > 0
        for d, (o, h, lo, c, n) in spot.get(coin, {}).items():
            k = (d - first).days
            if 0 <= k < len(days):
                arr["so"][k, j], arr["sh"][k, j], arr["sl"][k, j], arr["sc"][k, j] = o, h, lo, c
                stra[k, j] = n > 0
        for d, (s, n) in funding[coin].items():
            k = (d - first).days
            if 0 <= k < len(days) and n:
                arr["f"][k, j] = s
    history = History(days, coins, arr["po"], arr["ph"], arr["pl"], arr["pc"], ptr, arr["so"], arr["sh"], arr["sl"],
                      arr["sc"], stra, arr["f"], [np.datetime64(first_traded[c], "D") for c in coins],
                      [c in spot for c in coins])
    return Market(history, first_traded, start, end, why, spot_first, missing)


# ---- the simulation


@dataclass
class Run:
    days: list  # every day of the test
    equity: list  # the strategy's money after each day's close
    hold: list  # an equal share of the coins bought at the first day's open, after each day's close
    shadow: Shadow
    trading_days: int
    funding_paid: float
    funding_received: float


def _ffill(a):
    out = np.array(a, dtype=float, copy=True)
    for j in range(out.shape[1]):
        last = np.nan
        for k in range(out.shape[0]):
            if np.isfinite(out[k, j]):
                last = out[k, j]
            else:
                out[k, j] = last
    return out


def simulate(history: History, setting, spec, start: date, end: date, equity: float, slippage=None,
             running_per_day=0.0) -> Run:
    """The strategy run day by day on the shadow ledger, exactly as the bot books its own days: decide from the
    history through yesterday's close, fill at today's open, mark at today's close with today's funding.
    ``slippage``: a function of the day giving the ledger's slippage function (the hold-out replay passes the
    research's monthly tiers); by default the ledger's own."""
    coins = history.coins
    raw = history.raw
    perp_close, spot_close = _ffill(raw["perp_close"]), _ffill(raw["spot_close"])
    perp_open, spot_open = np.array(raw["perp_open"], dtype=float), np.array(raw["spot_open"], dtype=float)
    for a, close in ((perp_open, perp_close), (spot_open, spot_close)):
        prev = np.vstack([np.full((1, a.shape[1]), np.nan), close[:-1]])
        bad = ~np.isfinite(a)
        a[bad] = prev[bad]  # a day without an opening price opens at the last close
    funding = np.nan_to_num(np.asarray(raw["funding"], dtype=float), nan=0.0)
    first = history.dates[0]
    books = strategy_file.fresh_books(equity, start, setting, spec)
    shadow = Shadow(ShadowState(books, last_settled=_iso(start - timedelta(days=1))))
    builtin = config_for(setting) if spec is None else None
    k0 = int((np.datetime64(start, "D") - first) // np.timedelta64(1, "D"))
    days, eq, hold = [], [], []
    units = None
    trading_days, paid, received = 0, 0.0, 0.0
    d = start
    while d <= end:
        k = int((np.datetime64(d, "D") - first) // np.timedelta64(1, "D"))
        past = history.through(d - timedelta(days=1))
        dec = (decide(past, shadow.s.books, config=builtin) if spec is None
               else strategy_file.decide(past, shadow.s.books, spec))
        row = lambda a, k=k: {c: float(a[k, j]) for j, c in enumerate(coins)}  # noqa: E731
        trades_before = sum(b.trades for b in shadow.s.buckets.values())
        shadow.execute(dec, row(perp_open), row(spot_open), slippage=slippage(d) if slippage else None)
        trading_days += sum(b.trades for b in shadow.s.buckets.values()) > trades_before
        before = sum(b.funding for b in shadow.s.buckets.values())
        shadow.settle(d, row(perp_close), row(spot_close), row(funding), running_per_day=running_per_day)
        flow = sum(b.funding for b in shadow.s.buckets.values()) - before
        paid, received = paid + max(-flow, 0.0), received + max(flow, 0.0)
        if units is None:  # the comparison: an equal share of every coin, bought at the first day's open
            units = {c: equity / len(coins) / perp_open[k0, j] for j, c in enumerate(coins)}
        days.append(d)
        eq.append(shadow.s.books.equity)
        hold.append(sum(units[c] * perp_close[k, j] for j, c in enumerate(coins)))
        d += timedelta(days=1)
    return Run(days, eq, hold, shadow, trading_days, paid, received)


# ---- the numbers


def _ret(a, b):
    return b / a - 1.0 if a else float("nan")


def stretches(days, equity, start_equity):
    """The worst day, the calendar weeks (Monday to Sunday) and months, worst first, and the deepest fall."""
    before = [start_equity, *equity[:-1]]
    daily = [(d, _ret(a, b)) for d, a, b in zip(days, before, equity, strict=True)]
    weeks, months = {}, {}
    for i, d in enumerate(days):
        wk = d - timedelta(days=d.weekday())
        weeks.setdefault(wk, [i, i])[1] = i
        months.setdefault((d.year, d.month), [i, i])[1] = i
    week_rows = sorted(({"from": _iso(days[a]), "to": _iso(days[b]), "change": _ret(before[a], equity[b])}
                        for a, b in weeks.values()), key=lambda r: (r["change"], r["from"]))
    month_rows = sorted(({"month": f"{y:04d}-{m:02d}", "from": _iso(days[a]), "to": _iso(days[b]),
                          "change": _ret(before[a], equity[b])} for (y, m), (a, b) in months.items()),
                        key=lambda r: (r["change"], r["month"]))
    worst_day = min(daily, key=lambda x: (x[1], x[0]))
    peak, peak_day = start_equity, days[0]  # the starting money counts as a high on the first day
    fall = {"change": 0.0}
    for d, e in zip(days, equity, strict=True):
        if e > peak:
            peak, peak_day = e, d
        dd = e / peak - 1.0
        if dd < fall["change"]:
            fall = {"change": dd, "high_day": _iso(peak_day), "low_day": _iso(d), "high": peak, "low": e}
    if fall["change"] < 0:
        high_day, low_day = date.fromisoformat(fall["high_day"]), date.fromisoformat(fall["low_day"])
        back = next((d for d, e in zip(days, equity, strict=True) if d > low_day and e >= fall["high"]), None)
        fall["falling_days"] = (low_day - high_day).days
        fall["back_day"] = _iso(back) if back else None
        fall["lasted_days"] = ((back or days[-1]) - high_day).days
    return {"worst_day": {"day": _iso(worst_day[0]), "change": worst_day[1]}, "weeks": week_rows,
            "months": month_rows, "deepest_fall": fall}


# ---- the report


def _pct(x):
    return "n/a" if x is None or not math.isfinite(x) else f"{x * 100:+.1f}%"


def _usdc(x):
    return f"{x:,.2f} USDC"


def _fall_words(fall):
    if fall["change"] >= 0:
        return "It never fell below its starting money or a later high."
    s = (f"{_pct(fall['change'])}, from the high on {fall['high_day']} to the low on {fall['low_day']} "
         f"({fall['falling_days']} days of falling)")
    if fall["back_day"]:
        return s + f"; it was back at that high on {fall['back_day']}, {fall['lasted_days']} days after the high."
    return s + f"; it was not back at that high by the end of the test, {fall['lasted_days']} days after the high."


def build_report(label, words, setting, spec, run: Run, start_equity, market: Market, now=None):
    why = market.why
    now = now or datetime.now(timezone.utc)
    s = stretches(run.days, run.equity, start_equity)
    h = stretches(run.days, run.hold, start_equity)
    buckets = run.shadow.s.buckets
    fees = sum(b.fees for b in buckets.values())
    slip = sum(b.slippage for b in buckets.values())
    funding = sum(b.funding for b in buckets.values())
    trades = sum(b.trades for b in buckets.values())
    traded = sum(b.traded_notional for b in buckets.values())
    coins = list(market.history.coins)
    end_equity = run.equity[-1]
    doc = {
        "first_line": FIRST_LINE,
        "strategy": {"setting": setting, "words": words, "label": label,
                     "fingerprint": spec.fingerprint if spec else None},
        "coins": coins,
        "period": {"start": _iso(run.days[0]), "end": _iso(run.days[-1]), "days": len(run.days), "why": why},
        "start_equity": start_equity, "end_equity": end_equity, "result": _ret(start_equity, end_equity),
        "hold": {"end_equity": run.hold[-1], "result": _ret(start_equity, run.hold[-1]),
                 "worst_day": h["worst_day"], "worst_week": h["weeks"][0], "worst_month": h["months"][0],
                 "deepest_fall": h["deepest_fall"]},
        "worst_day": s["worst_day"], "worst_week": s["weeks"][0], "worst_month": s["months"][0],
        "worst_weeks": s["weeks"][:3], "deepest_fall": s["deepest_fall"],
        "costs": {"fees": fees, "slippage": slip, "funding_net": funding, "funding_paid": run.funding_paid,
                  "funding_received": run.funding_received},
        "trading": {"trades": trades, "traded_usdc": traded, "trading_days": run.trading_days},
        "assumptions": {"fill": "the day's opening price", "perp_fee": PERP_FEE, "spot_fee": SPOT_FEE,
                        "slippage": {**MODEL_SLIPPAGE, "other": DEFAULT_SLIPPAGE}, "funding": "Hyperliquid's hourly "
                        "funding, summed per day, on the position held through the day"},
        "spot_from": {c: _iso(d) for c, d in market.spot_first.items()},
        "missing_funding": market.missing_funding,
        "daily": [[_iso(d), e, x] for d, e, x in zip(run.days, run.equity, run.hold, strict=True)],
        "made": now.isoformat(timespec="seconds"),
    }
    lines = [FIRST_LINE, "", f"# {DISPLAY_NAME} backtest: {words}", ""]
    if spec:
        lines += [f"Strategy file {spec.name}, fingerprint {spec.fingerprint}: {strategy_file.explain(spec)}", ""]
    lines += [f"Period: {doc['period']['start']} to {doc['period']['end']}, {len(run.days)} days. {why}", ""]
    if market.spot_first and setting in ("carry", "mix"):
        lines += ["The carry trade needs the coins themselves, Hyperliquid's Unit tokens, which trade from "
                  + ", ".join(f"{d} ({c})" for c, d in sorted(market.spot_first.items(), key=lambda x: x[1]))
                  + "; before that the carry money waited in cash.", ""]
    hold_names = strategy_file._list_words(coins)
    lines += [
        "## The whole period", "",
        f"- Result: {_pct(doc['result'])}, from {_usdc(start_equity)} to {_usdc(end_equity)}.",
        f"- Holding an equal share of {hold_names} over the same days, without fees or funding: "
        f"{_pct(doc['hold']['result'])}.", "",
        "## The worst stretches", "",
        f"- Worst day: {s['worst_day']['day']}, {_pct(s['worst_day']['change'])} "
        f"(holding: worst day {h['worst_day']['day']}, {_pct(h['worst_day']['change'])}).",
        f"- Worst week: {s['weeks'][0]['from']} to {s['weeks'][0]['to']}, {_pct(s['weeks'][0]['change'])}.",
        f"- Worst month: {s['months'][0]['month']}, {_pct(s['months'][0]['change'])}.",
        "- The three worst weeks (Monday to Sunday):",
    ]
    lines += [f"  {n}. {w['from']} to {w['to']}: {_pct(w['change'])}" for n, w in enumerate(s["weeks"][:3], 1)]
    lines += [
        f"- The deepest fall from a high point: {_fall_words(s['deepest_fall'])}",
        f"- Holding the coins, the deepest fall: {_fall_words(h['deepest_fall'])}", "",
        "## Costs and trading", "",
        f"- Trading fees: {_usdc(fees)}. Slippage (the price moving against each order): {_usdc(slip)}.",
        f"- Funding: received {_usdc(run.funding_received)}, paid {_usdc(run.funding_paid)}, "
        f"{'net received' if funding >= 0 else 'net paid'} {_usdc(abs(funding))}.",
        f"- Trades: {trades} orders on {run.trading_days} of the {len(run.days)} days, {_usdc(traded)} traded in all "
        f"({traded / start_equity:,.1f} times the starting money).", "",
        "## How it was tested", "",
        f"- Starting money {_usdc(start_equity)}. Every order fills at the day's opening price, with fees of "
        f"{PERP_FEE * 100:g}% on perpetuals and {SPOT_FEE * 100:g}% on spot, and slippage of "
        f"{MODEL_SLIPPAGE['BTC'] * 100:g}% on BTC and ETH, {MODEL_SLIPPAGE['SOL'] * 100:g}% on SOL and "
        f"{DEFAULT_SLIPPAGE * 100:g}% on other coins: the same assumptions as the bot's own record of what the "
        "backtest would have made.",
        "- Prices and funding are Hyperliquid's own (its main exchange): daily candles, and hourly funding summed per "
        "day. A day without trades in a coin is left out of its signals, as in the bot itself.",
        "- It tested exactly this strategy as written; it did not try other settings to find better-looking ones.",
    ]
    if market.missing_funding:
        lines.append("- Days without funding records (counted as no funding): " + ", ".join(
            f"{c} {n}" for c, n in sorted(market.missing_funding.items())) + ".")
    return "\n".join(lines) + "\n", doc


def run(cfg, setting, start=None, end=None, market=None, today=None, out_dir=None, sleep=time.sleep, name=None):
    """Test a strategy and write its report. Returns (markdown path, json path, markdown text). ``name``: the report's
    file name instead of strategy and period (the chat door asks for its request's)."""
    from .venue.hyperliquid import HyperliquidMarketData

    label, words, coins, spot_coins, spec = strategy_parts(cfg, setting)
    today = today or datetime.now(timezone.utc).date()
    last_day = today - timedelta(days=1)
    if end and end > last_day:
        raise BacktestError(f"The last day to test can be {last_day} at the latest (the last closed day), not {end}.")
    if start and end and start > end:
        raise BacktestError(f"The start ({start}) is after the end ({end}).")
    market_data = market or HyperliquidMarketData("mainnet", None, tuple(dict.fromkeys(coins)))
    folder = Path(out_dir or cfg.state_path / "backtests")
    cache = PriceCache(folder / "cache", market_data, last_day, sleep=sleep)
    try:
        m = load_market(cache, market_data, tuple(coins), tuple(spot_coins), spec, last_day, start, end)
    except BacktestError:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever the exchange did, no report is written
        raise BacktestError(f"Hyperliquid did not give the past prices ({type(exc).__name__}); no report was written. "
                            "Try again in a few minutes.") from None
    equity = float(cfg.capital_cap_usdc)
    result = simulate(m.history, setting if spec is None else spec.setting, spec, m.start, m.end, equity)
    text, doc = build_report(label, words, setting, spec, result, equity, m)
    base = folder / (name or f"{label}-{m.start}-to-{m.end}")
    done = []
    for path, body in ((base.with_suffix(".json"), json.dumps(doc, indent=1)), (base.with_suffix(".md"), text)):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        done.append((tmp, path))
    for tmp, path in done:  # both written before either is put in place
        os.replace(tmp, path)
    return base.with_suffix(".md"), base.with_suffix(".json"), text


__all__ = ["FIRST_LINE", "BacktestError", "PriceCache", "build_report", "load_market", "period", "run", "simulate",
           "stretches"]
