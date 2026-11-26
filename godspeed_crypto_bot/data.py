"""Turn a venue's public market data into the strategy's History (Hyperliquid's own candles and funding).

Amendment A1 made Hyperliquid funding the deciding input; live, every input is Hyperliquid's: daily perp
candles for trend and the carry balance, daily Unit spot candles for the carry eligibility and the spot
leg's mark, and hourly funding settlements summed per UTC day the way the research summed them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np

from .strategy import History

DAY_MS = 86_400_000


class DataNotReady(RuntimeError):
    """Yesterday's candle or funding is not there yet (or never came): the run must not decide."""


def day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def funding_through_close(events, first: date, last: date):
    """Daily funding per UTC day: events in (00:00, 24:00], settlement times rounded to the minute.

    The research engine's rule (its funding_rate_sum_through_close): what a position held
    from the day's open to its close pays, including the settlement exactly at the close.
    Returns {day: (rate_sum, events)}.
    """
    out = {}
    for t, rate in events:
        settle = ((int(t) + 30_000) // 60_000) * 60_000
        d = date(1970, 1, 1) + timedelta(days=(settle - 1) // DAY_MS)
        if first <= d <= last:
            s, n = out.get(d, (0.0, 0))
            out[d] = (s + rate, n + 1)
    return out


@dataclass
class MarketSnapshot:
    """What one run read from the venue, besides the History."""

    day: date  # the decision day
    open_perp: dict = field(default_factory=dict)  # coin -> open of the decision day (the backtest's fill price)
    open_spot: dict = field(default_factory=dict)
    close_perp: dict = field(default_factory=dict)  # coin -> {day: close} for days the ledger may need to settle
    close_spot: dict = field(default_factory=dict)
    funding: dict = field(default_factory=dict)  # coin -> {day: rate}
    funding_events: dict = field(default_factory=dict)  # coin -> {day: count}
    warnings: list = field(default_factory=list)


def build_history(market, day: date, coins=("BTC", "ETH", "SOL"), history_days=400, funding_days=12):
    """History through ``day - 1`` and the decision day's opens.

    Refuses (DataNotReady) unless yesterday's perp candle for every coin has closed, and every carry coin
    has funding settlements on each of the last seven days.
    """
    end = day - timedelta(days=1)
    start = end - timedelta(days=history_days - 1)
    insts = market.instruments()
    n = (end - start).days + 1
    dates = np.arange(np.datetime64(start, "D"), np.datetime64(end, "D") + np.timedelta64(1, "D"))
    shape = (n, len(coins))
    arrays = {k: np.full(shape, np.nan) for k in (
        "perp_open", "perp_high", "perp_low", "perp_close", "spot_open", "spot_high", "spot_low", "spot_close",
        "funding")}
    perp_traded = np.zeros(shape, dtype=bool)
    spot_traded = np.zeros(shape, dtype=bool)
    first_trade, has_spot = [], []
    snap = MarketSnapshot(day)
    for j, coin in enumerate(coins):
        perp = insts.get((coin, "perp"))
        if perp is None:
            raise DataNotReady(f"{coin} perp is not listed on {market.network}")
        candles = market.daily_candles(perp, start, day)
        closed = {c.day: c for c in candles if c.day <= end}
        last = closed.get(end)
        if last is None or not last.closed:
            raise DataNotReady(f"{coin} perp candle for {end} is not closed yet")
        for c in closed.values():
            k = (c.day - start).days
            arrays["perp_open"][k, j], arrays["perp_high"][k, j] = c.open, c.high
            arrays["perp_low"][k, j], arrays["perp_close"][k, j] = c.low, c.close
            perp_traded[k, j] = c.trades > 0
        today = [c for c in candles if c.day == day]
        if today:
            snap.open_perp[coin] = today[0].open
        snap.close_perp[coin] = {c.day: c.close for c in closed.values()}
        first_trade.append(np.datetime64(min(closed), "D"))
        spot = insts.get((coin, "spot"))
        has_spot.append(spot is not None)
        if spot is not None:
            sc = market.daily_candles(spot, start, day)
            for c in sc:
                if c.day <= end:
                    k = (c.day - start).days
                    arrays["spot_open"][k, j], arrays["spot_high"][k, j] = c.open, c.high
                    arrays["spot_low"][k, j], arrays["spot_close"][k, j] = c.low, c.close
                    spot_traded[k, j] = c.trades > 0
                elif c.day == day:
                    snap.open_spot[coin] = c.open
            snap.close_spot[coin] = {c.day: c.close for c in sc if c.day <= end}
        f_first = end - timedelta(days=funding_days - 1)
        events = market.funding_history(coin, day_ms(f_first) - DAY_MS, day_ms(day) + 60_000)
        daily = funding_through_close(events, f_first, end)
        snap.funding[coin] = {d: v[0] for d, v in daily.items()}
        snap.funding_events[coin] = {d: v[1] for d, v in daily.items()}
        for d, (rate, _count) in daily.items():
            arrays["funding"][(d - start).days, j] = rate
        for k in range(7):
            d = end - timedelta(days=k)
            cnt = daily.get(d, (0.0, 0))[1]
            if cnt == 0:
                raise DataNotReady(f"{coin} has no funding settlements on {d}")
            if cnt != 24:
                snap.warnings.append(f"{coin} had {cnt} funding settlements on {d}, not 24")
    hist = History(dates, coins, arrays["perp_open"], arrays["perp_high"], arrays["perp_low"], arrays["perp_close"],
                   perp_traded, arrays["spot_open"], arrays["spot_high"], arrays["spot_low"], arrays["spot_close"],
                   spot_traded, arrays["funding"], first_trade, has_spot)
    return hist, snap
