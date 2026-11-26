"""The shadow ledger: the two virtual books run forward under the backtest's own assumptions.

This is what the lab's backtest would have produced on the days the bot runs: orders fill at the daily
open with the lab's fees and slippage tiers, positions are marked at each daily close, and perp funding is
the day's funding rate times the close. The account (live or paper) is compared against it; that
comparison is the point of the experiment (README, "What is measured").

Operation order follows the research engine's account and book execution; tests/test_parity.py replays the
whole hold-out through this ledger and requires the lab's recorded daily equity to the cent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .strategy import Books, Decision, with_balance_prices

PERP_FEE = 4.5e-4
SPOT_FEE = 7e-4
# The lab's slippage by monthly volume rank: BTC and ETH were ranks 1-2 (2 bps) and SOL rank 3-4 (8 bps) in
# every hold-out month, so the live model fixes those tiers.
MODEL_SLIPPAGE = {"BTC": 2e-4, "ETH": 2e-4, "SOL": 8e-4}
DEFAULT_SLIPPAGE = 15e-4


def slippage_for_rank(rank):
    if rank is not None and rank <= 2:
        return 2e-4
    if rank is not None and rank <= 10:
        return 8e-4
    return 15e-4


@dataclass
class Buckets:
    price_pnl: float = 0.0
    funding: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    running: float = 0.0
    trades: int = 0
    traded_notional: float = 0.0

    def to_dict(self):
        return dict(self.__dict__)


@dataclass
class ShadowState:
    """Virtual books plus what the ledger needs to keep marking them: the last mark per position."""

    books: Books
    marks: dict = field(default_factory=dict)  # "perp:BTC" / "spot:BTC" -> last mark price
    buckets: dict = field(default_factory=lambda: {"trend": Buckets(), "carry": Buckets()})
    last_settled: str | None = None  # the last day whose close and funding are booked

    def to_dict(self):
        return {"books": self.books.to_dict(), "marks": self.marks,
                "buckets": {k: v.to_dict() for k, v in self.buckets.items()}, "last_settled": self.last_settled}

    @classmethod
    def from_dict(cls, d):
        return cls(Books.from_dict(d["books"]), dict(d["marks"]),
                   {k: Buckets(**v) for k, v in d["buckets"].items()}, d.get("last_settled"))


class Shadow:
    def __init__(self, state: ShadowState, slippage=None, perp_fee=PERP_FEE, spot_fee=SPOT_FEE):
        self.s = state
        self.slippage = slippage or (lambda coin: MODEL_SLIPPAGE.get(coin, DEFAULT_SLIPPAGE))
        self.perp_fee, self.spot_fee = perp_fee, spot_fee

    # The lab's Account.mark_to / trade / fund, on a book: equity lives on the book state.

    def _book(self, name):
        return self.s.books.trend if name == "trend" else self.s.books.carry

    def _positions(self, name):
        b = self._book(name)
        if name == "trend":
            return {f"perp:{c}": q for c, q in b.q.items()}
        out = {f"perp:{c}": q for c, q in b.perp_q.items()}
        out.update({f"spot:{c}": q for c, q in b.spot_q.items()})
        return out

    def _mark_to(self, name, key, price, q):
        if q:
            pnl = q * (price - self.s.marks[key + "@" + name])
            self.s.buckets[name].price_pnl += pnl
            self._book(name).equity += pnl
        self.s.marks[key + "@" + name] = float(price)

    def _trade(self, name, key, q_old, dq, price, fee_rate, slip_rate):
        self._mark_to(name, key, price, q_old)
        if dq == 0:
            return
        notional = abs(dq) * price
        fee = notional * fee_rate * 1.0
        slip = notional * slip_rate * 1.0
        pen = notional * 0.0 * 1.0
        bk = self.s.buckets[name]
        bk.fees += fee
        bk.slippage += slip
        self._book(name).equity -= fee + slip + pen
        bk.traded_notional += notional
        bk.trades += 1

    def _fund(self, name, key, q, price, rate):
        if not q or not rate:
            return 0.0
        flow = -q * price * rate
        self.s.buckets[name].funding += flow
        self._book(name).equity += flow
        return flow

    def settle(self, day, close, spot_close, funding, running_per_day=0.0):
        """Book day ``day``: mark every position to that day's close and pay or receive its funding.

        close / spot_close / funding: {coin: value} for that day. Each book pays half the running cost.
        """
        for name in ("trend", "carry"):
            pos = self._positions(name)
            for c in sorted({k.split(":")[1] for k in pos}):
                sq = pos.get(f"spot:{c}", 0.0)
                if sq:
                    self._mark_to(name, f"spot:{c}", spot_close[c], sq)
                pq = pos.get(f"perp:{c}", 0.0)
                if pq:
                    self._mark_to(name, f"perp:{c}", close[c], pq)
                    self._fund(name, f"perp:{c}", pq, close[c], funding.get(c, 0.0))
        # The running cost is split over the books that run: half each for the mix, all of it for a single book.
        books = self.s.books
        names = {"trend": ("trend",), "carry": ("carry",)}.get(books.strategy, ("trend", "carry"))
        # A single choice owns the whole equity: what is left in the other book (the cost of closing it after
        # a change of choice) joins the running book once that one holds nothing.
        if len(names) == 1:
            if names[0] == "trend":
                idle_holds, idle, run = books.carry.perp_q or books.carry.spot_q, books.carry, books.trend
            else:
                idle_holds, idle, run = books.trend.q, books.trend, books.carry
            if not idle_holds:
                run.equity += idle.equity
                idle.equity = 0.0
        for name in names:
            share = running_per_day / len(names)
            self._book(name).equity -= share
            self.s.buckets[name].running += share
        self.s.last_settled = str(day)

    def execute(self, decision: Decision, perp_open, spot_open, slippage=None):
        """Fill the decision's orders at the day's open, as the lab does, and adopt its books.

        Returns the balance prices set for rebalanced carry coins (the perp fill price).
        """
        slip = slippage or self.slippage
        old = self.s.books
        # The decision's books carry the monthly reset (equity moved between the books, cost charged).
        old.trend.equity = decision.trend_equity
        old.carry.equity = decision.carry_equity
        new = decision.books_after
        for c in sorted(decision.trend_orders):
            q = old.trend.q.get(c, 0.0)
            target = decision.trend_orders[c]
            if target != q:
                self._trade("trend", f"perp:{c}", q, target - q, perp_open[c], self.perp_fee, slip(c))
        balance = {}
        for c in sorted(decision.carry_orders):
            target = decision.carry_orders[c]
            qp = old.carry.perp_q.get(c, 0.0)
            qs = old.carry.spot_q.get(c, 0.0)
            self._trade("carry", f"perp:{c}", qp, -target - qp, perp_open[c], self.perp_fee, slip(c))
            self._trade("carry", f"spot:{c}", qs, target - qs, spot_open[c], self.spot_fee, slip(c))
            if target > 0:
                balance[c] = float(perp_open[c])
        books = with_balance_prices(new, balance)
        books.trend.equity = old.trend.equity
        books.carry.equity = old.carry.equity
        # Positions closed to zero lose their mark, as the lab's Account deletes a zero position.
        for name in ("trend", "carry"):
            live = set()
            if name == "trend":
                live = {f"perp:{c}@trend" for c in books.trend.q}
            else:
                live = {f"perp:{c}@carry" for c in books.carry.perp_q} | {f"spot:{c}@carry" for c in books.carry.spot_q}
            for k in [k for k in self.s.marks if k.endswith("@" + name) and k not in live]:
                del self.s.marks[k]
        self.s.books = books
        return balance
