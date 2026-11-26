"""S3-MAJ: one pure decision function from daily history and the two virtual books to target positions.

Nothing in this module knows about an exchange, a key, an order or a clock. It is the strategy that
was chosen by the research behind this add-on, a long-history hold-out test: 50% S1-MAJ (trend on BTC and ETH perps) and 50% S2-HL-2x (spot-long, perp-short carry on
BTC, ETH and SOL), reset to 50/50 on the first day of each month.

VENDORED, NOT IMPORTED. The arithmetic below is copied from the engine of the research that chose this strategy (a
separate project, recorded by LAB_COMMIT and LAB_ENGINE_BLOB), keeping that engine's names and operation order,
because float results depend on the order of operations. tests/test_parity.py replays every day of that research's
hold-out through `decide` and requires the targets to equal the engine's, bit for bit. Why vendored: see README.

Timing: a decision for day t uses data through the close of day t-1 (00:00 UTC of day t).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np
import pandas as pd

LAB_COMMIT = "26bec5a44f2399fc4fde722966e4723621a5afc4"
LAB_ENGINE_BLOB = "0f25e3341f3d2765d858c3aee1ec31f8111a9f62"

ANNUAL_DAYS = 365
BAND_TOLERANCE = 1e-12
MIN_LISTING_DAYS = 120
JUMP_LIMIT = 0.5
TRANSFER_COST = 1e-4  # the lab's monthly 50/50 reset cost; live passes 0 (no money moves, see Books)


# --------------------------------------------------------------------------------------------
# Vendored from the research engine (names and operation order unchanged)


def liquidation_multiple(leverage, maintenance=0.005):
    """Isolated short: margin + Q (P0 - P) = maintenance * Q * P  ->  P = P0 (1 + 1/leverage) / (1 + maintenance)."""
    return (1 + 1 / leverage) / (1 + maintenance)


def clean_prices(open_, high, low, close, traded):
    """NaN every row without trades, and every row opening >50% away from the previous close."""
    o, h, lo, c = (np.array(a, dtype=float, copy=True) for a in (open_, high, low, close))
    with np.errstate(invalid="ignore"):
        valid = (
            np.asarray(traded, dtype=bool)
            & np.isfinite(o)
            & np.isfinite(h)
            & np.isfinite(lo)
            & np.isfinite(c)
            & (o > 0)
            & (c > 0)
        )
    breaks = []
    n_days, n_sym = c.shape
    prev_close = np.full(n_sym, np.nan)
    for t in range(n_days):
        with np.errstate(invalid="ignore", divide="ignore"):
            jump = valid[t] & np.isfinite(prev_close) & (np.abs(o[t] / prev_close - 1.0) > JUMP_LIMIT)
        if jump.any():
            valid[t] &= ~jump
            breaks.extend((t, int(i)) for i in np.flatnonzero(jump))
        prev_close = np.where(valid[t], c[t], np.nan)
    for a in (o, h, lo, c):
        a[~valid] = np.nan
    return o, h, lo, c, valid, breaks


def segment_start(valid):
    """Index of the first day of the continuous run of valid days containing each day (NaN if invalid)."""
    n_days, n_sym = valid.shape
    prev = np.vstack([np.zeros((1, n_sym), dtype=bool), valid[:-1]])
    starts = valid & ~prev
    idx = np.where(starts, np.arange(n_days, dtype=float)[:, None], np.nan)
    seg = pd.DataFrame(idx).ffill().to_numpy(copy=True)
    seg[~valid] = np.nan
    return seg


@dataclass(frozen=True)
class TrendConfig:
    lookbacks: tuple = (20, 60, 120)
    vol_window: int = 60
    cov_window: int = 90
    vol_target: float = 0.20
    coin_cap: float = 0.5
    major_cap: float = 1.0
    gross_cap: float = 2.0
    band: float = 0.02
    long_only: bool = False
    majors_only: bool = False


def trend_signal(close_col, seg_col, d, lookbacks):
    """Average of sign(close[d] / close[d-L] - 1); None unless every look-back lies in the current run."""
    start = seg_col[d]
    if not np.isfinite(start):
        return None
    total = 0.0
    for lb in lookbacks:
        if d - lb < start:
            return None
        total += float(np.sign(close_col[d] / close_col[d - lb] - 1.0))
    return total / len(lookbacks)


def shrunk_covariance(returns):
    """Annualized pairwise sample covariance, shrunk 50% toward its own diagonal."""
    cov = pd.DataFrame(returns).cov().to_numpy() * ANNUAL_DAYS
    return 0.5 * cov + 0.5 * np.diag(np.diag(cov))


def vol_scaled_weights(signals, returns_vol, returns_cov, vol_target, caps, gross_cap):
    """signal / vol, scaled to the target predicted vol, then per-coin caps, then the gross cap."""
    signals = np.asarray(signals, dtype=float)
    sigma = np.asarray(returns_vol, dtype=float).std(axis=0, ddof=1) * math.sqrt(ANNUAL_DAYS)
    if not np.all(np.isfinite(sigma)) or np.any(sigma <= 0):
        return None
    raw = signals / sigma
    cov = shrunk_covariance(returns_cov)
    var = float(raw @ cov @ raw)
    if not math.isfinite(var) or var <= 0:
        return None
    w = raw * vol_target / math.sqrt(var)
    caps = np.asarray(caps, dtype=float)
    w = np.clip(w, -caps, caps)
    gross = float(np.abs(w).sum())
    if gross > gross_cap:
        w = w * (gross_cap / gross)
    return w


def needs_trade(target_w, current_w, in_universe, band):
    """No-trade band: trade on a 2%-of-equity difference, a sign change (zero counts), or an exit."""
    if current_w != 0 and not in_universe:
        return True
    if np.sign(target_w) != np.sign(current_w):
        return True
    return abs(target_w - current_w) >= band - BAND_TOLERANCE


@dataclass(frozen=True)
class CarryConfig:
    mode: str = "HL"
    margin_leverage: float = 3.0
    notional_fraction: float = 0.75
    reset_move: float = 0.15
    hl_enter: float = 0.08
    hl_exit: float = 0.0
    entry_window: int = 7


def trailing_annualized(rates, d, i, days):
    """Mean daily funding over days d-days+1..d (up to the decision's close), times 365."""
    lo = max(0, d - days + 1)
    return float(np.mean(rates[lo : d + 1, i])) * ANNUAL_DAYS


def select_hl(active, ann7, eligible, coins, enter, exit_):
    """Enter at >= enter, stay while >= exit_ (hysteresis); ineligible coins are out."""
    new = []
    for i in coins:
        if not eligible.get(i, False):
            continue
        if i in active:
            if ann7[i] >= exit_:
                new.append(i)
        elif ann7[i] >= enter:
            new.append(i)
    return new


# --------------------------------------------------------------------------------------------
# The strategy as registered: S3-MAJ = 50% S1-MAJ + 50% S2-HL-2x


@dataclass(frozen=True)
class StrategyConfig:
    trend: TrendConfig = TrendConfig(majors_only=True)  # S1-MAJ
    # S2-HL-2x (amendment A2): short leg at 2x, notional at most 2/3 of the carry book's equity.
    carry: CarryConfig = CarryConfig(mode="HL", margin_leverage=2.0, notional_fraction=2 / 3)
    trend_coins: tuple = ("BTC", "ETH")  # the lab's MAJORS, in its order
    carry_coins: tuple = ("BTC", "ETH", "SOL")  # the lab's HL_CARRY_SYMBOLS, in its order
    min_listing_days: int = MIN_LISTING_DAYS
    reset_transfer_cost: float = TRANSFER_COST
    strategy: str = "mix"  # which of the two books run: "mix" (both, the lab's S3-MAJ), "trend" or "carry" alone


S3_MAJ = StrategyConfig()

STRATEGIES = ("mix", "trend", "carry")
DEFAULT_STRATEGY = "mix"
# What a person reads: "mix" is the lab's S3-MAJ, "trend" its S1-MAJ book alone, "carry" its S2-HL-2x book alone.
STRATEGY_WORDS = {
    "mix": "half trend, half carry",
    "trend": "trend on BTC and ETH",
    "carry": "carry on BTC, ETH and SOL",
}
STRATEGY_REFUSAL = 'strategy must be "mix", "trend" or "carry"'


def check_strategy(strategy):
    if strategy not in STRATEGIES:
        raise ValueError(f"{STRATEGY_REFUSAL}, not {strategy!r}")
    return strategy


def config_for(strategy) -> StrategyConfig:
    """The strategy configuration for a choice; "mix" is S3_MAJ itself, unchanged."""
    return replace(S3_MAJ, strategy=check_strategy(strategy))


def split_equity(strategy, total):
    """(trend book equity, carry book equity) for a choice: the whole money to the one book, or half and half."""
    if strategy == "trend":
        return total, 0.0
    if strategy == "carry":
        return 0.0, total
    return total / 2, total / 2


class History:
    """Daily wide arrays [day, coin] through the last closed day. Prices are NaN on days without a valid close.

    `funding` is the daily funding rate per coin over (00:00, 24:00] UTC (the lab's
    funding_rate_sum_through_close), positive meaning longs pay; NaN counts as 0, as in the lab.
    """

    def __init__(self, dates, coins, perp_open, perp_high, perp_low, perp_close, perp_traded,
                 spot_open, spot_high, spot_low, spot_close, spot_traded, funding, first_trade, has_spot):
        self.dates = np.asarray(dates, dtype="datetime64[D]")
        if len(self.dates) == 0:
            raise ValueError("history is empty")
        if len(self.dates) > 1 and np.any(np.diff(self.dates) != np.timedelta64(1, "D")):
            raise ValueError("history dates must be consecutive days")
        self.coins = tuple(coins)
        self.index = {c: i for i, c in enumerate(self.coins)}
        shape = (len(self.dates), len(self.coins))
        raw = {
            "perp_open": perp_open, "perp_high": perp_high, "perp_low": perp_low, "perp_close": perp_close,
            "spot_open": spot_open, "spot_high": spot_high, "spot_low": spot_low, "spot_close": spot_close,
            "funding": funding,
        }
        raw = {k: np.asarray(v, dtype=float) for k, v in raw.items()}
        raw["perp_traded"] = np.asarray(perp_traded, dtype=bool)
        raw["spot_traded"] = np.asarray(spot_traded, dtype=bool)
        for name, arr in raw.items():
            if arr.shape != shape:
                raise ValueError(f"{name} has shape {arr.shape}, expected {shape}")
        self.raw = raw
        self.first_trade = np.asarray(first_trade, dtype="datetime64[D]")
        self.has_spot = np.asarray(has_spot, dtype=bool)
        _, _, _, self.perp_close, self.perp_valid, self.perp_breaks = clean_prices(
            raw["perp_open"], raw["perp_high"], raw["perp_low"], raw["perp_close"], raw["perp_traded"]
        )
        _, _, _, self.spot_close, self.spot_valid, self.spot_breaks = clean_prices(
            raw["spot_open"], raw["spot_high"], raw["spot_low"], raw["spot_close"], raw["spot_traded"]
        )
        self.rates = np.nan_to_num(raw["funding"], nan=0.0)
        self.perp_segment = segment_start(self.perp_valid)
        with np.errstate(invalid="ignore", divide="ignore"):
            lr = np.full(shape, np.nan)
            lr[1:] = np.log(self.perp_close[1:] / self.perp_close[:-1])
        self.log_ret = lr

    @property
    def last_day(self) -> date:
        return pd.Timestamp(self.dates[-1]).date()

    def through(self, day) -> History:
        """The history as it was known at the close of ``day``.

        Every derived array is causal (a row depends only on earlier rows), so slicing them equals
        rebuilding from the raw rows; tests/test_strategy.py checks that.
        """
        k = int((np.datetime64(day, "D") - self.dates[0]) // np.timedelta64(1, "D"))
        if not 0 <= k < len(self.dates):
            raise IndexError(f"{day} is outside the history {self.dates[0]}..{self.dates[-1]}")
        out = object.__new__(History)
        out.dates = self.dates[: k + 1]
        out.coins = self.coins
        out.index = self.index
        out.raw = {key: v[: k + 1] for key, v in self.raw.items()}
        out.first_trade = self.first_trade
        out.has_spot = self.has_spot
        for name in ("perp_close", "perp_valid", "spot_close", "spot_valid", "rates", "perp_segment", "log_ret"):
            setattr(out, name, getattr(self, name)[: k + 1])
        out.perp_breaks = [b for b in self.perp_breaks if b[0] <= k]
        out.spot_breaks = [b for b in self.spot_breaks if b[0] <= k]
        return out


@dataclass
class TrendBookState:
    equity: float
    q: dict = field(default_factory=dict)  # coin -> signed perp quantity


@dataclass
class CarryBookState:
    equity: float
    perp_q: dict = field(default_factory=dict)  # coin -> perp quantity (negative: the short leg)
    spot_q: dict = field(default_factory=dict)  # coin -> spot quantity (the long leg)
    active: list = field(default_factory=list)
    last_set: list = field(default_factory=list)
    balance_price: dict = field(default_factory=dict)  # coin -> perp price at the last balance


@dataclass
class Books:
    """The two virtual books. ``start_day`` is the first decision day; the monthly reset applies after it."""

    trend: TrendBookState
    carry: CarryBookState
    start_day: date | None = None
    strategy: str = "mix"  # "trend" and "carry" put the whole equity in one book and leave the other empty

    @classmethod
    def fresh(cls, equity, start_day, strategy="mix"):
        trend, carry = split_equity(check_strategy(strategy), equity)
        return cls(TrendBookState(trend), CarryBookState(carry), start_day, strategy)

    @property
    def equity(self):
        return self.trend.equity + self.carry.equity

    def copy(self):
        t, c = self.trend, self.carry
        return Books(
            TrendBookState(t.equity, dict(t.q)),
            CarryBookState(c.equity, dict(c.perp_q), dict(c.spot_q), list(c.active), list(c.last_set),
                           dict(c.balance_price)),
            self.start_day,
            self.strategy,
        )

    def to_dict(self):
        t, c = self.trend, self.carry
        return {
            "start_day": str(self.start_day) if self.start_day else None,
            "strategy": self.strategy,
            "trend": {"equity": t.equity, "q": t.q},
            "carry": {"equity": c.equity, "perp_q": c.perp_q, "spot_q": c.spot_q, "active": c.active,
                      "last_set": c.last_set, "balance_price": c.balance_price},
        }

    @classmethod
    def from_dict(cls, d):
        t, c = d["trend"], d["carry"]
        start = date.fromisoformat(d["start_day"]) if d.get("start_day") else None
        return cls(
            TrendBookState(float(t["equity"]), {k: float(v) for k, v in t["q"].items()}),
            CarryBookState(float(c["equity"]), {k: float(v) for k, v in c["perp_q"].items()},
                           {k: float(v) for k, v in c["spot_q"].items()}, list(c["active"]), list(c["last_set"]),
                           {k: float(v) for k, v in c["balance_price"].items()}),
            start,
            d.get("strategy", "mix"),  # books written before the choice existed are the mix
        )


@dataclass
class Decision:
    day: date
    reset: bool
    trend_equity: float
    carry_equity: float
    trend_orders: dict  # coin -> target perp quantity, only coins the band says to trade (the lab's semantics)
    carry_orders: dict  # coin -> target spot quantity (perp = -target); 0 closes the pair
    carry_active: list
    trend_weights: dict
    carry_funding_7d: dict
    carry_eligible: dict
    scale: float  # account equity / virtual book equity
    books_after: Books  # the books once the orders are done; balance prices of rebalanced coins unset
    rebalanced: list  # carry coins whose balance price is set by this decision's fill

    def account_targets(self):
        """Net target per coin across the two books, scaled to the account: {coin: (perp, spot)}."""
        b = self.books_after
        coins = sorted(set(b.trend.q) | set(b.carry.perp_q) | set(b.carry.spot_q))
        out = {}
        for c in coins:
            perp = b.trend.q.get(c, 0.0) + b.carry.perp_q.get(c, 0.0)
            spot = b.carry.spot_q.get(c, 0.0)
            out[c] = (perp * self.scale, spot * self.scale)
        return out

    def to_dict(self):
        return {
            "day": str(self.day), "reset": self.reset, "trend_equity": self.trend_equity,
            "carry_equity": self.carry_equity, "trend_orders": self.trend_orders, "carry_orders": self.carry_orders,
            "carry_active": self.carry_active, "trend_weights": self.trend_weights,
            "carry_funding_7d": self.carry_funding_7d, "carry_eligible": self.carry_eligible, "scale": self.scale,
            "rebalanced": self.rebalanced, "books_after": self.books_after.to_dict(),
        }


def _age_ok(history, day, i, min_days):
    return (np.datetime64(day, "D") - history.first_trade[i]) >= np.timedelta64(min_days, "D")


def monthly_reset(books, day, transfer_cost):
    """The lab's S3 rule: on the first day of a month after the first day, both books go back to 50/50.

    Operation order is the lab's (run(): transfer, then charge half the cost to each account).
    """
    if books.strategy != "mix" or books.start_day is None or not day > books.start_day or day.day != 1:
        return False
    a, b = books.trend, books.carry
    target = (a.equity + b.equity) / 2
    moved = a.equity - target
    a.equity += -moved
    b.equity += moved
    cost = abs(moved) * transfer_cost * 1.0
    a.equity -= cost / 2
    b.equity -= cost / 2
    return True


def _decide_trend(history, book, day, cfg, coins, min_days):
    """The research engine's trend book decision for the majors, on a book state instead of an account."""
    d = len(history.dates) - 1
    equity = book.equity
    idx = history.index
    cands = [idx[c] for c in coins if c in idx and _age_ok(history, day, idx[c], min_days)]
    in_universe = set(cands)
    held = sorted(idx[c] for c, q in book.q.items() if q and c in idx)
    orders, weights = {}, {}
    if equity <= 0:
        orders = {i: 0.0 for i in held}
    else:
        ids, sig = [], []
        for i in cands:
            s = trend_signal(history.perp_close[:, i], history.perp_segment[:, i], d, cfg.lookbacks)
            if s is None:
                continue
            if cfg.long_only:
                s = max(s, 0.0)
            if s == 0:
                continue
            if not history.perp_segment[d, i] <= d - cfg.vol_window:
                continue
            ids.append(i)
            sig.append(s)
        targets = {}
        if ids:
            rv = history.log_ret[d - cfg.vol_window + 1 : d + 1][:, ids]
            rc = history.log_ret[max(0, d - cfg.cov_window + 1) : d + 1][:, ids]
            caps = np.array([cfg.major_cap if history.coins[i] in coins else cfg.coin_cap for i in ids])
            w = vol_scaled_weights(sig, rv, rc, cfg.vol_target, caps, cfg.gross_cap)
            if w is not None:
                targets = {i: float(x) for i, x in zip(ids, w, strict=True)}
        weights = {history.coins[i]: w for i, w in targets.items()}
        for i in sorted(in_universe | set(held)):
            price = history.perp_close[d, i]
            if not np.isfinite(price):
                continue
            q = book.q.get(history.coins[i], 0.0)
            w = targets.get(i, 0.0)
            cw = q * price / equity if q else 0.0
            if needs_trade(w, cw, i in in_universe, cfg.band):
                orders[i] = w * equity / price
    return {history.coins[i]: v for i, v in orders.items()}, weights


def _decide_carry(history, book, day, cfg, coins, min_days):
    """The research engine's carry book decision in its Hyperliquid mode, on a book state instead of an account."""
    d = len(history.dates) - 1
    equity = book.equity
    idx = history.index
    ids = [idx[c] for c in coins if c in idx]
    elig = {
        i: bool(_age_ok(history, day, i, min_days) and history.has_spot[i] and history.perp_valid[d, i]
                and history.spot_valid[d, i])
        for i in ids
    }
    ann7 = {i: trailing_annualized(history.rates, d, i, cfg.entry_window) for i in ids}
    active = [idx[c] for c in book.active if c in idx]
    new = select_hl(active, ann7, elig, ids, cfg.hl_enter, cfg.hl_exit)
    if equity <= 0:
        new = []
    held = sorted(
        {idx[c] for c, q in book.perp_q.items() if q and c in idx} | {idx[c] for c, q in book.spot_q.items() if q and c in idx}
    )
    orders = {i: 0.0 for i in held if i not in new}
    if new:
        notional = cfg.notional_fraction * equity / len(new)
        last = {idx[c] for c in book.last_set if c in idx}
        changed = set(new) != last or any(book.perp_q.get(history.coins[i], 0.0) == 0 for i in new)
        for i in new:
            price = history.perp_close[d, i]
            if changed or abs(price / book.balance_price[history.coins[i]] - 1.0) > cfg.reset_move:
                orders[i] = notional / price
    names = history.coins
    return (
        {names[i]: v for i, v in orders.items()},
        [names[i] for i in new],
        {names[i]: v for i, v in ann7.items()},
        {names[i]: v for i, v in elig.items()},
    )


def decide(history: History, books: Books, equity: float | None = None, config: StrategyConfig = S3_MAJ,
           day: date | None = None) -> Decision:
    """Target positions for day ``history.last_day + 1``.

    Inputs: the daily history through yesterday's close, the two virtual books as they stand after
    yesterday, and the account's strategy equity. The books are never mutated; the result carries the
    books after the decision and the scale that maps the virtual books onto the account
    (``equity / books.equity``, 1 when ``equity`` is None). Sizing is linear in equity and the
    no-trade band compares weights, so the scaled targets are exactly what the strategy decides for
    that equity.
    """
    expected = history.last_day.toordinal() + 1
    if day is None:
        day = date.fromordinal(expected)
    elif day.toordinal() != expected:
        raise ValueError(f"a decision for {day} needs history through {date.fromordinal(day.toordinal() - 1)}, "
                         f"got {history.last_day}")
    b = books.copy()
    if b.strategy != config.strategy:
        # The choice changed: the whole virtual equity moves to the chosen book(s) and the books' own sizing
        # turns the difference into orders (a rebalance to the new targets, today). No transfer cost: nothing
        # moves between real accounts, and the real orders carry their own costs.
        b.trend.equity, b.carry.equity = split_equity(config.strategy, b.equity)
        b.strategy = config.strategy
    reset = monthly_reset(b, day, config.reset_transfer_cost)
    trend_orders, weights = _decide_trend(history, b.trend, day, config.trend, config.trend_coins,
                                          config.min_listing_days)
    carry_orders, new_active, ann7, elig = _decide_carry(history, b.carry, day, config.carry, config.carry_coins,
                                                         config.min_listing_days)
    trend_equity, carry_equity = b.trend.equity, b.carry.equity

    after = b.copy()
    for c, q in trend_orders.items():
        if q:
            after.trend.q[c] = q
        else:
            after.trend.q.pop(c, None)
    rebalanced = []
    for c, q in carry_orders.items():
        if q > 0:
            after.carry.perp_q[c] = -q
            after.carry.spot_q[c] = q
            rebalanced.append(c)
        else:
            after.carry.perp_q.pop(c, None)
            after.carry.spot_q.pop(c, None)
            after.carry.balance_price.pop(c, None)
    after.carry.active = list(new_active)
    after.carry.last_set = list(new_active)
    total = books.equity
    scale = 1.0 if equity is None else (equity / total if total > 0 else 0.0)
    return Decision(day, reset, trend_equity, carry_equity, trend_orders, carry_orders, list(new_active), weights,
                    ann7, elig, scale, after, rebalanced)


def with_balance_prices(books: Books, prices: dict) -> Books:
    """Set the balance price of each rebalanced carry coin (the lab: the perp fill price of the balance)."""
    out = books.copy()
    for c, px in prices.items():
        if c in out.carry.perp_q:
            out.carry.balance_price[c] = float(px)
    return out


__all__ = [
    "DEFAULT_STRATEGY", "S3_MAJ", "STRATEGIES", "STRATEGY_WORDS", "Books", "CarryBookState", "CarryConfig", "Decision", "History", "StrategyConfig", "TrendBookState",
    "TrendConfig", "check_strategy", "config_for", "decide", "monthly_reset", "split_equity", "with_balance_prices",
]
