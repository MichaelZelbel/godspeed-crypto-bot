"""Turning account targets into orders, and orders into fills.

Netting: the two virtual books may both hold a coin's perp (trend long, carry short). The account holds
one perp position per coin, so the order is the difference between the net target and what the account
holds. Spot is the carry book's alone.

A carry pair (that coin's spot order and perp order on the same run) goes to the venue together: on
Hyperliquid as two signed actions back to back, spot first (it refuses spot and perp orders in one action).
Each leg then retries its remainder. If a leg still
filled less than ``pair_fill_ratio`` of its size, every fill of that pair is reversed (the account goes
back to where it was for that coin) and the owner is told. Orders never re-run blindly: every size is
target minus actual, read from the account just before, so a second run on the same day only does
what the first did not.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

NON_RETRYABLE = ("Insufficient", "Reduce only", "minimum value", "MinTradeNtl", "Invalid", "not approved",
                 "does not exist", "User or API Wallet")


def is_agent_failure(message):
    """Signing refused because the API wallet is expired, pruned or unknown to Hyperliquid."""
    m = message or ""
    return ("does not exist" in m or "expired" in m or "pruned" in m or "not approved" in m) and (
        "API Wallet" in m or "API wallet" in m or "agent" in m.lower())


@dataclass
class Leg:
    coin: str
    inst: object  # venue Instrument
    size: float  # signed: positive buys
    mid: float
    purpose: str = "rebalance"
    pair: str | None = None
    reduce_only: bool = False
    order_id: int | None = None
    filled: float = 0.0  # signed
    cost: float = 0.0  # sum of |filled| x price
    fee: float = 0.0
    attempts: list = field(default_factory=list)
    status: str = "planned"

    @property
    def market(self):
        return self.inst.market

    @property
    def is_buy(self):
        return self.size > 0

    @property
    def remaining(self):
        return self.size - self.filled

    @property
    def avg_px(self):
        return self.cost / abs(self.filled) if self.filled else None

    @property
    def fill_ratio(self):
        return abs(self.filled) / abs(self.size) if self.size else 1.0


@dataclass
class Plan:
    legs: list
    skipped: list  # (coin, market, size, mid, reason)
    clipped: list


def cloid_for(*parts) -> str:
    """A client order id (16 bytes) derived from the run day, the instrument, the purpose and the attempt."""
    return "0x" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


def plan_orders(targets, account, instruments, mids, cfg, coins=("BTC", "ETH", "SOL")):
    """Orders that move the account to ``targets`` ({coin: (perp, spot)}), with the size limits applied.

    Skips any order under ``min_order_usdc`` (Hyperliquid refuses under $10). An order above
    ``max_order_usdc`` is clipped; a carry pair is clipped by the same factor on both legs.
    """
    legs, skipped, clipped = [], [], []
    min_usd, max_usd = cfg.limits.min_order_usdc, cfg.max_order()
    for coin in coins:
        perp_target, spot_target = targets.get(coin, (0.0, 0.0))
        pi = instruments.get((coin, "perp"))
        si = instruments.get((coin, "spot"))
        found = []
        if pi is not None:
            d = pi.round_size(perp_target - account.perp_size(coin))
            if d:
                found.append((pi, d))
        if si is not None:
            d = si.round_size(spot_target - account.spot_size(si.token))
            if d:
                found.append((si, d))
        kept = []
        for inst, d in found:
            mid = mids[inst.name]
            if abs(d) * mid < min_usd:
                skipped.append((coin, inst.market, d, mid, f"under ${min_usd:.0f}"))
            else:
                kept.append((inst, d, mid))
        if not kept:
            continue
        factor = min(1.0, *(max_usd / (abs(d) * mid) for _, d, mid in kept))
        pair = f"{coin}-pair" if len(kept) == 2 else None
        for inst, d, mid in kept:
            size = d
            if factor < 1.0:
                size = inst.round_size(d * factor)
                clipped.append((coin, inst.market, d, size, mid))
            if size:
                legs.append(Leg(coin, inst, size, mid, pair=pair))
    return Plan(legs, skipped, clipped)


class Executor:
    def __init__(self, cfg, venue, store, day, run_id=None, sleep=time.sleep, purpose="rebalance"):
        self.cfg, self.venue, self.store, self.day, self.run_id = cfg, venue, store, day, run_id
        self.sleep = sleep
        self.purpose = purpose
        self.mids = None
        self.day_notional = store.traded_notional_on(day)
        self.agent_failure = None  # set when Hyperliquid refuses the signer: the caller halts and alerts

    def _mid(self, inst, fresh=True):
        if self.mids is None or fresh:
            self.mids = self.venue.market.mids()
        return self.mids[inst.name]

    def _slippage(self, inst, unwind=False):
        if unwind:
            return self.cfg.limits.unwind_slippage
        return self.cfg.limits.perp_slippage if inst.market == "perp" else self.cfg.limits.spot_slippage

    def record(self, leg, status=None):
        side = "buy" if leg.is_buy else "sell"
        if leg.order_id is None:
            leg.order_id = self.store.add_order(self.run_id, self.day, leg.purpose, leg.coin, leg.market, side,
                                                abs(leg.size), leg.mid, status or "sent", pair=leg.pair)
        return leg.order_id

    def finish(self, leg, status, reason=None):
        slip = None
        if leg.filled and leg.mid:
            px = leg.avg_px
            slip = (px / leg.mid - 1.0) * 1e4 * (1 if leg.is_buy else -1)  # positive: worse than the decision mid
        leg.status = status
        self.store.finish_order(leg.order_id, status, abs(leg.filled), leg.avg_px, leg.fee, slip, reason)

    def run_group(self, legs, unwind=False):
        """Send the legs together (venue.place_ioc_batch), then retry each remainder. Returns the legs (filled, cost, fee updated)."""
        for leg in legs:
            self.record(leg)
        for attempt in range(1, self.cfg.limits.retries + 1):
            live = []
            if self.agent_failure:
                break
            for leg in legs:
                rem = leg.inst.round_size(leg.remaining)
                if not rem or leg.status.startswith("stopped"):
                    continue
                mid = self._mid(leg.inst, fresh=not live)
                if abs(rem) * mid < self.cfg.limits.min_order_usdc:
                    continue  # the venue refuses orders this small; the residual stays for the next run
                if not unwind and self.day_notional + abs(rem) * mid > self.cfg.max_day() + 1e-9:
                    leg.status = "stopped"
                    self.finish(leg, "stopped_day_cap", f"daily cap ${self.cfg.max_day():.0f} reached")
                    continue
                slip = self._slippage(leg.inst, unwind)
                limit = leg.inst.round_price(mid * (1 + slip) if rem > 0 else mid * (1 - slip))
                cloid = cloid_for(self.day, self.purpose, leg.inst.key, leg.order_id, attempt)
                live.append((leg, rem, mid, limit, cloid))
            if not live:
                break
            results = self._place([(leg.inst, rem > 0, abs(rem), limit, cloid, leg.reduce_only)
                                   for leg, rem, mid, limit, cloid in live])
            for (leg, rem, mid, limit, cloid), res in zip(live, results, strict=True):
                if res.filled and res.avg_px is None:
                    res.avg_px = mid  # filled but the price is unknown (status read back by cloid): mid until fills sync
                self.store.add_attempt(leg.order_id, attempt, cloid, abs(rem), limit, mid, res)
                leg.attempts.append(res)
                if res.filled:
                    signed = res.filled if rem > 0 else -res.filled
                    leg.filled += signed
                    leg.cost += res.filled * res.avg_px
                    leg.fee += res.fee or 0.0
                    self.day_notional += res.filled * res.avg_px
                if res.status == "rejected" and any(s in (res.error or "") for s in NON_RETRYABLE):
                    leg.status = "stopped"
                if is_agent_failure(res.error):
                    self.agent_failure = res.error
            if self.agent_failure:
                for leg in legs:
                    leg.status = "stopped"
                break
            if all(leg.status.startswith("stopped") or not leg.inst.round_size(leg.remaining) for leg in legs):
                break
            self.sleep(0.5 * attempt)
        return legs

    def _place(self, orders):
        """One signed batch when the venue supports it, else one by one."""
        batch = getattr(self.venue, "place_ioc_batch", None)
        if batch is not None and len(orders) > 1:
            return batch(orders)
        return [self.venue.place_ioc(inst, is_buy, size, limit, cloid, reduce_only)
                for inst, is_buy, size, limit, cloid, reduce_only in orders]

    def execute_plan(self, plan):
        """Pairs first (each leg pair sent together), then single legs. Returns a list of pair failures."""
        failures = []
        pairs = {}
        singles = []
        for leg in plan.legs:
            if leg.pair:
                pairs.setdefault(leg.pair, []).append(leg)
            else:
                singles.append(leg)
        for name, legs in pairs.items():
            self.run_group(legs)
            if all(leg.fill_ratio >= self.cfg.limits.pair_fill_ratio for leg in legs):
                for leg in legs:
                    self.finish(leg, "filled" if leg.fill_ratio >= 0.999 else "partial")
                continue
            failures.append(self.unwind_pair(name, legs))
        for leg in singles:
            self.run_group([leg])
            if leg.status == "stopped" and leg.filled == 0:
                self.finish(leg, "failed", leg.attempts[-1].error if leg.attempts else None)
            elif leg.status != "stopped_day_cap":
                self.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "unfilled"),
                            None if leg.filled else (leg.attempts[-1].error if leg.attempts else None))
        return failures

    def unwind_pair(self, name, legs):
        """A carry leg failed: reverse every fill of the pair so the coin is back where it started."""
        for leg in legs:
            self.finish(leg, "failed_leg" if leg.fill_ratio < self.cfg.limits.pair_fill_ratio else "unwound",
                        leg.attempts[-1].error if leg.attempts and leg.attempts[-1].error else None)
        reverse = []
        for leg in legs:
            if leg.filled:
                mid = self._mid(leg.inst)
                size = leg.inst.round_size(-leg.filled)
                if size and abs(size) * mid >= self.cfg.limits.min_order_usdc:
                    reverse.append(Leg(leg.coin, leg.inst, size, mid, purpose="unwind", pair=name + "-unwind"))
        if reverse:
            self.run_group(reverse, unwind=True)
            for leg in reverse:
                self.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "failed"))
        residual = {}
        for leg in legs:
            back = sum(r.filled for r in reverse if r.inst is leg.inst)
            left = leg.filled + back
            if abs(left) * leg.mid >= self.cfg.limits.min_order_usdc:
                residual[leg.inst.key] = left
        return {"pair": name, "coin": legs[0].coin, "legs": {leg.inst.key: (leg.size, leg.filled) for leg in legs},
                "unwound": not residual, "residual": residual}

    # ---- USDC between the spot and perp balances (standard mode: two wallets)

    def fund_spot_buys(self, plan, account):
        need = 0.0
        for leg in plan.legs:
            if leg.market == "spot" and leg.is_buy:
                slip = self.cfg.limits.spot_slippage
                need += abs(leg.size) * leg.mid * (1 + slip + 0.001)
        if need <= 0:
            return 0.0
        short = need - account.spot_usdc_free + 1.0
        if short <= 0:
            return 0.0
        amount = min(short, max(0.0, account.perp_withdrawable - 1.0))
        if amount > 0:
            ok = self.venue.transfer_usdc(round(amount, 2), to_perp=False)
            self.store.add_guard_action("transfer_to_spot", None, amount, ok, {"reason": "spot buys", "need": need})
            return amount if ok else 0.0
        return 0.0

    def balance_before_trading(self, plan, account, reserve):
        """Spot buys get their USDC from perp; otherwise spot USDC beyond the reserve goes to perp first.

        Deposits and the switch out of a unified account leave USDC in the spot balance, where perp orders
        cannot use it. After every run the sweep leaves only the reserve in spot, so on an ordinary day this
        moves nothing.
        """
        slip = self.cfg.limits.spot_slippage
        need = sum(abs(leg.size) * leg.mid * (1 + slip + 0.001) for leg in plan.legs
                   if leg.market == "spot" and leg.is_buy)
        if need - account.spot_usdc_free + 1.0 > 0:
            return self.fund_spot_buys(plan, account)
        excess = account.spot_usdc_free - need - reserve
        if excess >= 1.0 and any(leg.market == "perp" for leg in plan.legs):
            amount = math.floor(excess * 100) / 100  # whole cents, never more than is there
            ok = self.venue.transfer_usdc(amount, to_perp=True)
            self.store.add_guard_action("transfer_to_perp", None, amount, ok, {"reason": "spot USDC before trading"})
            return -amount if ok else 0.0
        return 0.0

    def sweep_spot_usdc(self, reserve):
        acct = self.venue.account()
        amount = acct.spot_usdc_free - reserve
        if amount >= 1.0:
            ok = self.venue.transfer_usdc(round(amount - 0.005, 2), to_perp=True)
            self.store.add_guard_action("transfer_to_perp", None, amount, ok, {"reason": "sweep after trading"})
            return amount if ok else 0.0
        return 0.0
