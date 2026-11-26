"""A simulated account on real market data. Nothing here signs or sends anything.

dry-run: every intended order "fills" at the mid moved by the backtest's slippage model, with the
         backtest's fees. The account then holds what the strategy intended, so tomorrow's intended
         orders are computed from it (including orders skipped as too small).
paper:   IOC orders fill against the live order book (walking the levels inside the limit price),
         with Hyperliquid's base taker fees; funding is the actual hourly Hyperliquid rate on the
         positions held, priced at the mid when it is booked.

Simplifications (both modes): fees are charged in USDC; perp margin is cross at the configured
leverage with maintenance 1 / (2 x max leverage), as Hyperliquid's tables give for these sizes.

unified=True simulates Hyperliquid's unified account: one USDC balance pays for spot buys and collateralizes
the perps, and there is nothing to transfer (a transfer is an error).
"""

from __future__ import annotations

import time

from ..ledger import DEFAULT_SLIPPAGE, MODEL_SLIPPAGE
from .base import AccountState, OrderResult, Position, Venue

FEES = {"perp": 4.5e-4, "spot": 7e-4}


class SimulatedVenue(Venue):
    signs = False

    def __init__(self, market, store, fill_model="book", start_usdc=0.0, leverage=3, now=time.time, unified=False):
        super().__init__(market)
        self.unified = unified
        if fill_model not in ("book", "model"):
            raise ValueError("fill_model must be 'book' (paper) or 'model' (dry-run)")
        self.store, self.fill_model, self.leverage, self.now = store, fill_model, leverage, now
        self.st = store.get("sim_account")
        if self.st is None:
            self.st = {"perp_cash": float(start_usdc), "spot_usdc": 0.0, "positions": {}, "spot": {},
                       "last_funding_ms": int(self.now() * 1000), "leverage": {}}
            self._save()
            ts = int(self.now() * 1000)
            store.add_venue_ledger(ts, "simulated_deposit", float(start_usdc), f"sim-deposit-{ts}", None, "USDC",
                                   float(start_usdc), "in")
        self._mids = None

    def _save(self):
        self.store.put("sim_account", self.st)

    def _mid_map(self, fresh=False):
        if self._mids is None or fresh:
            self._mids = self.market.mids()
        return self._mids

    def _inst(self, coin, market):
        return self.market.instruments()[(coin, market)]

    def mid(self, inst, fresh=False):
        return self._mid_map(fresh)[inst.name]

    def perp_marks(self):
        return self.market.perp_marks()

    def _maint_rate(self, coin):
        lev = self._inst(coin, "perp").max_leverage or 20
        return 1.0 / (2 * lev)

    def _listed(self, coin, mids):
        inst = self.market.instruments().get((coin, "perp"))
        return inst is not None and inst.name in mids

    def account(self):
        mids = self._mid_map(fresh=True)
        positions, upnl, maint, margin = {}, 0.0, 0.0, 0.0
        for coin, p in self.st["positions"].items():
            if not p["size"]:
                continue
            listed = self._listed(coin, mids)  # a coin the exchange no longer lists: held at its entry price
            m = mids[self._inst(coin, "perp").name] if listed else p["entry"]
            pnl = p["size"] * (m - p["entry"])
            notional = p["size"] * m
            upnl += pnl
            maint += abs(notional) * self._maint_rate(coin) if listed else 0.0
            margin += abs(notional) / self.leverage
            positions[coin] = Position(coin, p["size"], p["entry"], pnl, abs(notional) / self.leverage, None, notional)
        value = self.st["perp_cash"] + upnl
        if self.unified:  # the one USDC balance (cash, as Hyperliquid reports it in the spot state)
            spot = {"USDC": (self.st["perp_cash"], 0.0)}
        else:
            spot = {"USDC": (self.st["spot_usdc"], 0.0)}
        spot.update({tok: (q, 0.0) for tok, q in self.st["spot"].items() if q})
        return AccountState(value, max(0.0, value - margin), maint, margin, positions, spot,
                            "simulated-unified" if self.unified else "simulated")

    def _fill(self, inst, is_buy, size, limit_px):
        mid = self.mid(inst, fresh=True)
        if self.fill_model == "model":
            slip = MODEL_SLIPPAGE.get(inst.coin, DEFAULT_SLIPPAGE)
            px = mid * (1 + slip if is_buy else 1 - slip)
            if (is_buy and px > limit_px) or (not is_buy and px < limit_px):
                return 0.0, None
            return size, px
        bids, asks = self.market.l2_book(inst)
        levels = asks if is_buy else bids
        left, cost = size, 0.0
        for px, sz in levels:
            if (is_buy and px > limit_px) or (not is_buy and px < limit_px):
                break
            take = min(left, sz)
            cost += take * px
            left -= take
            if left <= 1e-12:
                break
        filled = size - max(left, 0.0)
        filled = inst.round_size(filled)
        return (filled, cost / (size - max(left, 0.0))) if filled > 0 else (0.0, None)

    def place_ioc(self, inst, is_buy, size, limit_px, cloid, reduce_only=False):
        size = abs(size)
        if inst.market == "spot":
            tok = inst.token
            if not is_buy and self.st["spot"].get(tok, 0.0) < size - 1e-12:
                return OrderResult("rejected", size, error="Insufficient spot balance")
        if reduce_only and inst.market == "perp":
            cur = self.st["positions"].get(inst.coin, {}).get("size", 0.0)
            if cur == 0 or (cur > 0) == is_buy:
                return OrderResult("rejected", size, error="Reduce only order would increase position")
            size = min(size, abs(cur))
        filled, px = self._fill(inst, is_buy, size, limit_px)
        if not filled:
            return OrderResult("unfilled", size, error="Order could not immediately match against any resting orders.")
        notional = filled * px
        fee = notional * FEES[inst.market]
        closed_pnl = 0.0
        if inst.market == "spot":
            tok = inst.token
            cash = "perp_cash" if self.unified else "spot_usdc"
            if is_buy:
                available = self.account().perp_withdrawable if self.unified else self.st["spot_usdc"]
                if available < notional + fee - 1e-9:
                    return OrderResult("rejected", size, error="Insufficient spot USDC balance")
                self.st[cash] -= notional + fee
                self.st["spot"][tok] = self.st["spot"].get(tok, 0.0) + filled
            else:
                self.st[cash] += notional - fee
                self.st["spot"][tok] = self.st["spot"].get(tok, 0.0) - filled
        else:
            prev = dict(self.st["positions"].get(inst.coin, {"size": 0.0, "entry": px}))
            p = self.st["positions"].setdefault(inst.coin, {"size": 0.0, "entry": px})
            dq = filled if is_buy else -filled
            if prev["size"] and (prev["size"] > 0) != (dq > 0):
                closed_pnl = min(abs(dq), abs(prev["size"])) * (px - prev["entry"]) * (1 if prev["size"] > 0 else -1)
            new = p["size"] + dq
            if p["size"] and (p["size"] > 0) != (dq > 0):  # reducing or flipping: realize PnL on the closed part
                closed = min(abs(dq), abs(p["size"]))
                self.st["perp_cash"] += closed * (px - p["entry"]) * (1 if p["size"] > 0 else -1)
                if abs(dq) > abs(p["size"]):
                    p["entry"] = px
            elif p["size"] == 0:
                p["entry"] = px
            else:
                p["entry"] = (p["entry"] * abs(p["size"]) + px * abs(dq)) / abs(new)
            p["size"] = round(new, 10)
            self.st["perp_cash"] -= fee
            if p["size"] == 0:
                del self.st["positions"][inst.coin]
            acc = self.account()
            if acc.total_margin_used > acc.perp_value + 1e-9 and not reduce_only and abs(new) > abs(new - dq):
                # undo: not enough margin (Hyperliquid would reject the order)
                self.st = self.store.get("sim_account")
                return OrderResult("rejected", size, error="Insufficient margin to place order.")
        self._save()
        ts = int(self.now() * 1000)
        self.store.add_venue_fill(ts, inst.coin, inst.market, inst.name, "buy" if is_buy else "sell", px, filled,
                                  fee, "USDC", closed_pnl, "simulated", None, None, f"sim-{cloid}", cloid)
        status = "filled" if filled >= size - 1e-12 else "partial"
        return OrderResult(status, size, filled, px, fee, None)

    def transfer_usdc(self, amount, to_perp):
        if self.unified:
            raise RuntimeError("a unified account has one USDC balance; nothing may transfer")
        if amount <= 0:
            return True
        if to_perp:
            if self.st["spot_usdc"] < amount - 1e-9:
                return False
            self.st["spot_usdc"] -= amount
            self.st["perp_cash"] += amount
        else:
            if self.account().perp_withdrawable < amount - 1e-9:
                return False
            self.st["perp_cash"] -= amount
            self.st["spot_usdc"] += amount
        self._save()
        ts = int(self.now() * 1000)
        self.store.add_venue_ledger(ts, "accountClassTransfer", amount, f"sim-transfer-{ts}-{amount}", None, "USDC",
                                    amount, "spot->perp" if to_perp else "perp->spot")
        return True

    def set_leverage(self, coin, leverage):
        self.st["leverage"][coin] = leverage
        self._save()

    def sync_funding(self, now_ms=None):
        """Book the actual Hyperliquid funding on the simulated perp positions since the last sync."""
        now_ms = int(now_ms or self.now() * 1000)
        last = int(self.st.get("last_funding_ms", now_ms))
        booked = []
        if now_ms > last:
            for coin, p in list(self.st["positions"].items()):
                if not p["size"] or (coin, "perp") not in self.market.instruments():
                    continue
                inst = self._inst(coin, "perp")
                events = self.market.funding_history(coin, last + 1, now_ms)
                px = self.mid(inst, fresh=not booked)
                for t, rate in events:
                    if not last < t <= now_ms:
                        continue
                    usdc = -p["size"] * px * rate
                    self.st["perp_cash"] += usdc
                    self.store.add_funding(t, coin, usdc, p["size"], rate, "simulated")
                    booked.append((t, coin, usdc))
            self.st["last_funding_ms"] = now_ms
            self._save()
        return booked

    def funding_payments(self, start_ms, end_ms):
        return [dict(r) for r in self.store.funding_between(start_ms, end_ms)]
