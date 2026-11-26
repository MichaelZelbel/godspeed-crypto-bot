"""The testnet self-test: the mechanics a daily run cannot reach on testnet, done once and recorded.

Testnet only. It refuses unless the configuration is live on testnet AND the venue signs for testnet
(not mainnet, testnet API URL). Every step is one row in ``selftest_steps``; every order goes through the
same Executor (and so the same orders/attempts records) as the daily run:

1. Balance mode: read, never set. The bot runs on a unified account (one USDC balance) or on separate spot
   and perp balances ('disabled'); anything else fails this step and steps 2 to 4 do not trade.
2. Separate balances only: 15 USDC between the spot and perp balances and back with agentSendAsset, the action
   built from the API docs that the SDK lacks. Skipped on a unified account, where nothing is transferred.
3. A spot buy and sell of about $12 through the carry pair's path: the spot leg and a small perp short go
   to Hyperliquid together (Executor.run_group -> place_ioc_batch: two signed actions back to back, spot first,
   because Hyperliquid refuses spot and perp orders in one action); on separate balances the
   USDC moves before and after exactly as the daily run moves it. Testnet's Unit tokens have no usable book
   (UBTC is missing, UETH and USOL quote far from their perps), so the spot token is the strategy's own if its
   book is usable, else another testnet token with a two-sided book. Slippage against the mid is recorded.
4. A small perp IOC open and close (the single-order path the trend legs use), with the 3x cross leverage
   the daily run sets.
5. withdraw3 and usdSend to another address, signed with the AGENT key. Both must be REJECTED by
   Hyperliquid and the master's balances must not move; the exact error texts are recorded. Hyperliquid
   answers "Must deposit before performing actions. User: <the agent's address>": a user-signed action is
   attributed to whoever signed it, so the agent's withdraw and send act for the agent's own empty address,
   never for the owner's account. Hyperliquid's API page says the same: API wallets act "without having
   withdrawal permissions".
6. Hyperliquid's own fills, funding and ledger read back into the records, and the tax export built from
   them with the venue's ids (transaction hash, order id, trade id).
7. The Unit peg guard on testnet's prices: a carry entry on every coin that has spot is offered to the same
   restriction the daily run applies; every coin whose spot is more than 1% off its perp must be refused.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
import time
from datetime import datetime, timezone

from hyperliquid.utils.error import ClientError

from . import export, safety
from .config import API_URLS
from .execution import Executor, Leg, Plan

log = logging.getLogger(__name__)

SINK = "0x000000000000000000000000000000000000dEaD"  # "another address": neither the master nor the bot
TRANSFER_USDC = 15.0
SPOT_USD = 12.5
PERP_USD = 12.0
MAX_SPOT_SPREAD = 0.03
EXTRA_SPOT_TOKENS = ("HYPE", "PURR")  # testnet tokens with real two-sided books, tried after the strategy's own
SPOT_FEE_ALLOWANCE = 0.002  # spot fees are paid in the received token
SELLBACK_MARGIN = 1.05  # what is sold back must clear the minimum order by this factor
MAX_SPOT_USD = 4 * SPOT_USD  # the leg may grow to clear the minimum, never beyond this
WITHDRAWABLE_TOLERANCE = 0.5  # USDC: price drift on open positions; the refused actions asked for 2 and 1
LEDGER_LOOKBACK_MS = 60_000  # the baseline read before the actions reaches this far back
OUT_TYPES = ("withdraw", "internalTransfer", "subAccountTransfer", "spotTransfer", "vaultWithdraw")


class SelftestRefused(RuntimeError):
    pass


def refusal(cfg, venue=None):
    """Why the self-test may not run here (None when it may). Mainnet is always refused."""
    if cfg.network != "testnet":
        return f"selftest runs on testnet only; this configuration is {cfg.network}"
    if cfg.mode != "live":
        return f"selftest needs mode = 'live' on testnet; this configuration is {cfg.mode!r}"
    if venue is not None:
        if not getattr(venue, "signs", False):
            return "the venue does not sign"
        if getattr(venue, "is_mainnet", True) or getattr(venue.market, "network", None) != "testnet":
            return "the venue signs for mainnet"
        if getattr(venue.market, "base_url", None) != API_URLS["testnet"]:
            return f"the venue talks to {getattr(venue.market, 'base_url', None)}, not testnet"
    return None


def _ceil_size(inst, usd, px):
    step = 10 ** -inst.sz_decimals
    return round(math.ceil(usd / px / step - 1e-9) * step, inst.sz_decimals)


def spot_leg_size(inst, mid, min_order, spot_usd=SPOT_USD, max_usd=MAX_SPOT_USD):
    """The smallest size, in whole steps of the token, of about ``spot_usd`` or more, that still sells back for at
    least ``min_order`` (with a margin) after the fee taken in the token and the rounding down to the size step.
    Returns (size, sellable) or None when no size within ``max_usd`` does."""
    step = 10 ** -inst.sz_decimals
    need = min_order * SELLBACK_MARGIN / mid / (1 - SPOT_FEE_ALLOWANCE)  # tokens to buy so the sellable part clears it
    size = max(_ceil_size(inst, spot_usd, mid), round(math.ceil(need / step - 1e-9) * step, inst.sz_decimals))
    for _ in range(4):  # rounding down of the sellable part can need one more step or two
        if size * mid > max_usd:
            return None
        sellable = inst.round_size(size * (1 - SPOT_FEE_ALLOWANCE))
        if sellable * mid >= min_order * SELLBACK_MARGIN:
            return size, sellable
        size = round(size + step, inst.sz_decimals)
    return None


def _reduces(held_before, opened):
    """Whether closing what the selftest opened may be sent reduce-only. With a position held before on the other
    side, the opening trade netted against it and a reduce-only closing order would be refused (it would grow
    that position), so the close is an ordinary order of the same size."""
    return held_before == 0 or (held_before > 0) == (opened > 0)


def _account_brief(acct):
    return {"perp_value": acct.perp_value, "withdrawable": acct.perp_withdrawable, "mode": acct.mode,
            "positions": {c: p.size for c, p in acct.positions.items()},
            "spot": {t: v[0] for t, v in acct.spot.items() if v[0]}}


def _wait(fn, until, sleep, tries=10, delay=1.0):
    value = fn()
    for _ in range(tries):
        if until(value):
            return value
        sleep(delay)
        value = fn()
    return value


def _leg_brief(leg):
    """Size, fill, average price and the slippage against the decision mid (bps, positive = worse)."""
    slip = None
    if leg.filled and leg.mid and leg.avg_px:
        slip = round((leg.avg_px / leg.mid - 1.0) * 1e4 * (1 if leg.is_buy else -1), 2)
    return {"size": leg.size, "filled": leg.filled, "mid": leg.mid, "avg_px": leg.avg_px, "slippage_bps": slip,
            "fee": leg.fee, "order_id": leg.order_id, "attempts": len(leg.attempts),
            "oids": [a.oid for a in leg.attempts if a.oid]}


def _error_text(exc):
    if hasattr(exc, "error_message"):
        return (f"HTTP {getattr(exc, 'status_code', '?')} {getattr(exc, 'error_code', None) or ''} "
                f"{exc.error_message}").replace("  ", " ")
    return f"{type(exc).__name__}: {exc}"


class Selftest:
    def __init__(self, cfg, venue, store, sleep=time.sleep, clock=time.time, spot_choice=None):
        reason = refusal(cfg, venue)
        if reason:
            raise SelftestRefused(reason)
        self.cfg, self.venue, self.store = cfg, venue, store
        self.sleep, self.clock = sleep, clock
        self.spot_choice = spot_choice
        self.session = datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.day = datetime.fromtimestamp(clock(), tz=timezone.utc).date()
        self.steps = []
        self.positions_before = {}
        self.t0_ms = int(clock() * 1000)
        self.unified = False

    def record(self, step, name, ok, detail):
        self.store.add_selftest_step(self.session, step, name, ok, detail)
        self.steps.append({"step": step, "name": name, "ok": bool(ok), **detail})
        log.info("selftest step %s %s: %s", step, name, "ok" if ok else "FAILED")
        return ok

    def executor(self, spot_slippage=None):
        cfg = self.cfg
        if spot_slippage is not None:
            cfg = dataclasses.replace(cfg, limits=dataclasses.replace(cfg.limits, spot_slippage=spot_slippage))
        return Executor(cfg, self.venue, self.store, self.day, None, sleep=self.sleep, purpose="selftest")

    # ---- 1. balance mode

    def balance_mode(self):
        """Read the balance mode; the bot never changes it (no agentSetAbstraction)."""
        from .venue.hyperliquid import SUPPORTED_MODES

        mode = self.venue.mode()
        self.unified = mode == "unifiedAccount"
        meaning = {"unifiedAccount": "one USDC balance pays for spot and collateralizes the perps: no transfers",
                   "disabled": "separate spot and perp balances: USDC moves between them with agentSendAsset"}
        return self.record(1, "balance mode", mode in SUPPORTED_MODES,
                           {"mode": mode, "meaning": meaning.get(mode, "not supported by the bot")})

    # ---- 2. agentSendAsset both ways (separate balances only)

    def usdc_round_trip(self):
        """15 USDC from the side that has it to the other and back (deposits land in spot, the faucet in perp)."""
        if self.unified:
            return self.record(2, "agentSendAsset between spot and perp, both ways", True,
                               {"skipped": "unified account: one USDC balance, nothing to transfer"})
        acct = self.venue.account()
        spot0 = acct.spot_usdc
        from_perp = acct.perp_withdrawable >= TRANSFER_USDC + 1.0
        out = {"amount": TRANSFER_USDC, "first": "perp->spot" if from_perp else "spot->perp", "spot_usdc_before": spot0}
        if not from_perp and acct.spot_usdc_free < TRANSFER_USDC:
            return self.record(2, "agentSendAsset between spot and perp, both ways", False,
                               {**out, "error": "neither balance holds 15 USDC", "account": _account_brief(acct)})
        sign = 1 if from_perp else -1
        target1 = spot0 + sign * TRANSFER_USDC
        r1 = self.venue.send_usdc_between_balances(TRANSFER_USDC, to_perp=not from_perp)
        a1 = _wait(self.venue.account, lambda a: abs(a.spot_usdc - target1) < 1e-6, self.sleep)
        ok1 = isinstance(r1, dict) and r1.get("status") == "ok" and abs(a1.spot_usdc - target1) < 1e-6
        out.update(first_response=r1, spot_usdc_after_first=a1.spot_usdc)
        r2 = self.venue.send_usdc_between_balances(TRANSFER_USDC, to_perp=from_perp) if ok1 else None
        a2 = _wait(self.venue.account, lambda a: abs(a.spot_usdc - spot0) < 1e-6, self.sleep) if ok1 else a1
        ok2 = isinstance(r2, dict) and r2.get("status") == "ok" and abs(a2.spot_usdc - spot0) < 1e-6
        out.update(back_response=r2, spot_usdc_after_back=a2.spot_usdc)
        return self.record(2, "agentSendAsset between spot and perp, both ways", ok1 and ok2, out)

    # ---- 3. a spot buy and sell in the carry pair's batch

    def spot_instrument(self, name):
        """A USDC spot pair by its token ("HYPE") or pair name ("@1035", "PURR/USDC"); None if not listed."""
        from .venue.base import Instrument

        sm = self.venue.market.spot_meta()
        by_index = {t["index"]: t for t in sm["tokens"]}
        usdc = next((t for t in sm["tokens"] if t["name"] == "USDC"), None)
        for u in sm["universe"]:
            base = by_index.get(u["tokens"][0])
            if usdc is None or u["tokens"][1] != usdc["index"] or base is None:
                continue
            if name in (u["name"], base["name"]):
                szd = int(base["szDecimals"])
                return Instrument(base["name"], "spot", u["name"], szd, 8 - szd, base["name"])
        return None

    def spot_candidates(self):
        """Spot instruments to try: the strategy's own first, then other testnet tokens with a USDC pair."""
        out = [i for (_c, m), i in sorted(self.venue.market.instruments().items()) if m == "spot"]
        for name in EXTRA_SPOT_TOKENS:
            inst = self.spot_instrument(name)
            if inst is not None and all(i.name != inst.name for i in out):
                out.append(inst)
        if self.spot_choice:
            chosen = [i for i in out if self.spot_choice in (i.token, i.name)]
            out = chosen or [i for i in (self.spot_instrument(self.spot_choice),) if i is not None]
        return out

    def pick_spot(self):
        """The first candidate with a two-sided book, a spread under 3%, and a size that sells back above $11."""
        mids = self.venue.market.mids()
        tried = []
        for inst in self.spot_candidates():
            bids, asks = self.venue.market.l2_book(inst)
            if not bids or not asks:
                tried.append({"spot": f"{inst.token} {inst.name}", "why": "one-sided book"})
                continue
            bid, ask = bids[0][0], asks[0][0]
            mid = mids.get(inst.name, (bid + ask) / 2)
            spread = (ask - bid) / mid
            sized = spot_leg_size(inst, mid, self.cfg.limits.min_order_usdc)
            size, sellable = sized if sized else (_ceil_size(inst, SPOT_USD, mid), 0.0)
            why = None
            if spread > MAX_SPOT_SPREAD:
                why = f"spread {spread:.1%} (bid {bid}, ask {ask})"
            elif sized is None:
                why = (f"no size up to {MAX_SPOT_USD:g} USD sells back above the "
                       f"{self.cfg.limits.min_order_usdc:g} USD minimum after the fee and the size step")
            elif asks[0][1] < size or bids[0][1] < sellable:
                why = "top of book too thin"
            if why:
                tried.append({"spot": f"{inst.token} {inst.name}", "why": why})
                continue
            return inst, {"bid": bid, "ask": ask, "mid": mid, "spread": spread, "size": size}, tried
        return None, None, tried

    def spot_through_batch(self):
        inst, book, tried = self.pick_spot()
        if inst is None:
            return self.record(3, "spot buy and sell paired with a perp leg (the carry pair's path)", False,
                               {"error": "no testnet spot token with a usable book", "tried": tried})
        insts = self.venue.market.instruments()
        pi = insts[("ETH", "perp")]
        mids = self.venue.market.mids()
        slip = min(0.05, max(self.cfg.limits.spot_slippage, book["spread"] / 2 + 0.005))
        ex = self.executor(spot_slippage=slip)
        self.venue.set_leverage("ETH", self.cfg.limits.leverage)
        acct = self.venue.account()
        held0, perp0 = acct.spot_size(inst.token), acct.perp_size("ETH")
        # The carry pair's shape: the perp leg and the spot leg, sent together.
        perp_leg = Leg("ETH", pi, -_ceil_size(pi, PERP_USD, mids[pi.name]), mids[pi.name], purpose="selftest",
                       pair="selftest-open")
        spot_leg = Leg(inst.coin, inst, book["size"], mids[inst.name], purpose="selftest", pair="selftest-open")
        batches = []
        real_batch = self.venue.place_ioc_batch

        def batch_spy(orders):
            results = real_batch(orders)
            batches.append({"orders": [{"instrument": i.name, "buy": b, "size": sz, "limit": px}
                                       for i, b, sz, px, _c, _r in orders],
                            "results": [{"status": r.status, "filled": r.filled, "oid": r.oid, "error": r.error}
                                        for r in results],
                            "signed_actions": list(getattr(self.venue, "last_batch_actions", []))})
            return results

        self.venue.place_ioc_batch = batch_spy
        try:
            funded = 0.0 if self.unified else ex.balance_before_trading(Plan([perp_leg, spot_leg], [], []), acct,
                                                                        self.cfg.guard.spot_usdc_reserve)
            ex.run_group([perp_leg, spot_leg])
            for leg in (perp_leg, spot_leg):
                ex.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "unfilled"))
            acct = self.venue.account()
            bought = acct.spot_size(inst.token) - held0
            shorted = acct.perp_size("ETH") - perp0
            close = []
            if pi.round_size(-shorted):
                close.append(Leg("ETH", pi, pi.round_size(-shorted), mids[pi.name], purpose="selftest",
                                 pair="selftest-close", reduce_only=_reduces(perp0, shorted)))
            if bought > 0 and inst.round_size(-bought):
                close.append(Leg(inst.coin, inst, inst.round_size(-bought), mids[inst.name], purpose="selftest",
                                 pair="selftest-close"))
            if close:
                ex.run_group(close)
                for leg in close:
                    ex.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "unfilled"))
        finally:
            self.venue.__dict__.pop("place_ioc_batch", None)
        swept = 0.0 if self.unified else ex.sweep_spot_usdc(0.0)
        after = self.venue.account()
        detail = {
            "spot": {"token": inst.token, "pair": inst.name, **book}, "tried_first": tried,
            "spot_slippage_used": slip, "funded_spot_usdc": funded, "swept_back_usdc": swept,
            "open": {leg.inst.key: _leg_brief(leg) for leg in (perp_leg, spot_leg)},
            "close": {leg.inst.key: _leg_brief(leg) for leg in close},
            "signed_batches": batches,
            "dust_left": {inst.token: after.spot_size(inst.token) - held0},
            "eth_perp_left": after.perp_size("ETH") - perp0,
        }
        ok = (spot_leg.fill_ratio >= 0.9 and perp_leg.fill_ratio >= 0.9 and len(close) == 2
              and all(leg.fill_ratio >= 0.9 for leg in close)
              and bool(batches) and len(batches[0]["orders"]) == 2
              and abs(after.perp_size("ETH") - perp0) < 10 ** -pi.sz_decimals / 2
              and inst.round_size(after.spot_size(inst.token) - held0) == 0)
        return self.record(3, "spot buy and sell paired with a perp leg (the carry pair's path)", ok, detail)

    # ---- 4. a perp IOC open and close

    def perp_open_close(self):
        pi = self.venue.market.instruments()[("BTC", "perp")]
        self.venue.set_leverage("BTC", self.cfg.limits.leverage)
        ex = self.executor()
        pos0 = self.venue.account().perp_size("BTC")
        mid = self.venue.market.mids()[pi.name]
        open_leg = Leg("BTC", pi, _ceil_size(pi, PERP_USD, mid), mid, purpose="selftest")
        if not self.unified:
            ex.balance_before_trading(Plan([open_leg], [], []), self.venue.account(),
                                      self.cfg.guard.spot_usdc_reserve)
        ex.run_group([open_leg])
        ex.finish(open_leg, "filled" if open_leg.fill_ratio >= 0.999 else ("partial" if open_leg.filled else "unfilled"))
        opened = self.venue.account().perp_size("BTC") - pos0
        close_leg = None
        if pi.round_size(-opened):
            mid = self.venue.market.mids()[pi.name]
            close_leg = Leg("BTC", pi, pi.round_size(-opened), mid, purpose="selftest",
                            reduce_only=_reduces(pos0, opened))
            ex.run_group([close_leg])
            ex.finish(close_leg, "filled" if close_leg.fill_ratio >= 0.999
                      else ("partial" if close_leg.filled else "unfilled"))
        left = self.venue.account().perp_size("BTC") - pos0
        detail = {"leverage": f"{self.cfg.limits.leverage}x cross",
                  "open": _leg_brief(open_leg), "close": None if close_leg is None else _leg_brief(close_leg),
                  "btc_perp_left": left}
        ok = open_leg.fill_ratio >= 0.9 and close_leg is not None and abs(left) < 10 ** -pi.sz_decimals / 2
        return self.record(4, "perp IOC open and close", ok, detail)

    # ---- 5. what the agent key must NOT be able to do

    @staticmethod
    def _row_key(r):
        return r.get("hash") or json.dumps(r, sort_keys=True, default=str)

    def _money_out(self, rows, known):
        """Ledger rows not in ``known`` (by hash) that take money out of the account: a withdrawal or a transfer to
        someone else, or anything to the sink. A "send" to the account itself (step 2's balance transfer) is not."""
        master = self.venue.master.lower()
        out = []
        for r in rows:
            d = r.get("delta", {}) if isinstance(r, dict) else {}
            if self._row_key(r) in known:
                continue
            dest = str(d.get("destination", "")).lower()
            if (d.get("type") in OUT_TYPES or dest == SINK.lower()
                    or (d.get("type") == "send" and dest not in ("", master))):
                out.append(r)
        return out

    def forbidden_moves(self):
        ex = self.venue.exchange
        before = self.venue.account()

        def now_ms():
            return int(self.clock() * 1000)

        window_start = now_ms() - LEDGER_LOOKBACK_MS  # the same start for both reads

        # What the ledger already holds, read first: only entries that appear after the two actions count. If the
        # ledger cannot be read, "nothing moved out" is unproven and the step fails.
        try:
            known = {self._row_key(r) for r in self.venue.ledger_updates(window_start, now_ms() + 60_000)}
            ledger_error = None
        except Exception as exc:  # noqa: BLE001
            known, ledger_error = set(), f"ledger before the actions: {_error_text(exc)}"
        tries = (("withdraw3", lambda: ex.withdraw_from_bridge(2.0, SINK)),
                 ("usdSend", lambda: ex.usd_transfer(1.0, SINK)))
        results = {}
        for name, call in tries:
            indeterminate = False
            try:
                resp = call()
                error = None
            except Exception as exc:  # noqa: BLE001 - the refusal IS the result
                resp, error = None, _error_text(exc)
                # Only an answer from the exchange (a 4xx) is a refusal; a timeout or a 5xx leaves it unknown.
                indeterminate = not isinstance(exc, ClientError)
            accepted = error is None and isinstance(resp, dict) and resp.get("status") == "ok"
            if error is None:
                error = resp.get("response") if isinstance(resp, dict) else str(resp)
            found = re.search(r"User: (0x[0-9a-fA-F]{40})", str(error or ""))
            attributed = found.group(1).lower() if found else None
            results[name] = {"rejected": not accepted and not indeterminate,
                             "message": error if not accepted else None,
                             "response": resp, "amount": 2.0 if name == "withdraw3" else 1.0,
                             "destination": SINK, "signed_by": self.venue.agent_address,
                             "attributed_to": attributed,
                             "attributed_to_the_agents_own_address": attributed == self.venue.agent_address.lower()}
            if indeterminate:
                results[name]["indeterminate"] = "no answer from the exchange: not counted as refused"
        self.sleep(2)
        after = self.venue.account()
        try:
            ledger = self.venue.ledger_updates(window_start, now_ms() + 60_000)
        except Exception as exc:  # noqa: BLE001
            ledger, ledger_error = [], ledger_error or f"ledger after the actions: {_error_text(exc)}"
        moved_out = self._money_out(ledger, known)
        # Equity drifts with open positions and funding, so it is not the proof. The proof is the refusal and a
        # ledger without a new withdrawal or send; the withdrawable USDC is a further check that it did not fall
        # by what was asked for (2 and 1 USDC; the tolerance is price drift on positions held before).
        withdrawable_drop = before.perp_withdrawable - after.perp_withdrawable
        unchanged = withdrawable_drop < WITHDRAWABLE_TOLERANCE
        ok = (all(r["rejected"] for r in results.values()) and unchanged and not moved_out
              and ledger_error is None)
        own = all(r["attributed_to_the_agents_own_address"] for r in results.values())
        if ok and own:
            meaning = ("Hyperliquid attributed both agent-signed actions to the agent's OWN empty address, not to "
                       "the owner's account: an API wallet's user-signed actions only ever act for itself, so the "
                       "agent key can never withdraw from or send out of the account (Hyperliquid: API wallets act "
                       "'without having withdrawal permissions').")
        elif ok:
            meaning = "refused, and nothing left the account"
        else:
            meaning = "NOT REFUSED or money moved or unproven: stop and look before anything else"
        detail = {**results, "meaning": meaning, "master_before": _account_brief(before),
                  "master_after": _account_brief(after), "withdrawable_drop": round(withdrawable_drop, 6),
                  "ledger_entries_moving_money_out": moved_out}
        if ledger_error:
            detail["ledger_error"] = ledger_error
        return self.record(5, "withdraw3 and usdSend signed by the agent are refused", ok, detail)

    # ---- 6. the venue's records, and the export built from them

    def records_and_export(self):
        now_ms = int(self.clock() * 1000)
        start = self.t0_ms - 5 * 60_000
        counts = self.venue.sync_records(self.store, start, now_ms + 60_000)
        funding_3d = self.venue.funding_payments(now_ms - 3 * 86_400_000, now_ms + 60_000)
        fills = self.store.db.execute(
            "SELECT * FROM venue_fills WHERE mode=? AND network=? AND time_ms>=? ORDER BY time_ms",
            (self.store.mode, self.store.network, start)).fetchall()
        ledger = self.store.db.execute(
            "SELECT * FROM venue_ledger WHERE mode=? AND network=? AND time_ms>=? ORDER BY time_ms",
            (self.store.mode, self.store.network, start)).fetchall()
        cloids = {r["cloid"] for r in self.store.db.execute(
            "SELECT a.cloid FROM attempts a JOIN orders o ON o.id=a.order_id WHERE o.purpose='selftest' "
            "AND o.mode=? AND o.network=? AND o.day=?", (self.store.mode, self.store.network, str(self.day)))}
        matched = [f for f in fills if f["cloid"] in cloids]
        rows = [r for r in export.rows(self.store, self.day, self.day)
                if r["timestamp_utc"] >= export._ts(start)]
        trade_rows = [r for r in rows if r["category"] == "trade"]
        with_ids = [r for r in trade_rows if r["venue_tx_hash"] and r["venue_order_id"] and r["venue_trade_id"]]
        transfers = [r for r in rows if r["category"] == "transfer"]
        detail = {
            "synced": counts, "funding_payments_last_3_days": len(funding_3d),
            "fills": len(fills), "fills_matching_selftest_cloids": len(matched),
            "ledger_rows": len(ledger), "ledger_kinds": sorted({r["kind"] for r in ledger}),
            "export_rows": len(rows), "export_trade_rows": len(trade_rows), "export_trade_rows_with_venue_ids":
                len(with_ids), "export_transfer_rows": len(transfers),
            "sample_trade": {k: with_ids[0][k] for k in ("timestamp_utc", "asset", "side", "quantity", "price_usdc",
                                                         "venue_tx_hash", "venue_order_id", "venue_trade_id",
                                                         "client_order_id")} if with_ids else None,
            "sample_transfer": {k: transfers[0][k] for k in ("timestamp_utc", "side", "quantity", "venue_tx_hash",
                                                             "description")} if transfers else None,
        }
        ok = (bool(matched) and bool(with_ids) and len(with_ids) == len(trade_rows)
              and (bool(transfers) or self.unified))  # a unified account makes no transfers
        return self.record(6, "venue fills, funding and ledger in the records and the export", ok, detail)

    # ---- 7. the Unit peg guard on testnet's prices

    def peg_guard(self):
        insts = self.venue.market.instruments()
        peg = safety.observe_peg(self.store, self.venue, self.clock())
        gaps = {c: s.get("gap") for c, s in peg.items()}
        marks = self.venue.market.perp_marks()
        acct = self.venue.account()
        offer = {}
        for coin, m in insts:
            if m == "spot" and coin in marks:
                q = 50.0 / marks[coin]
                offer[coin] = (-q, q)
        targets, notes = safety.restrict_targets(offer, acct, insts, peg, False, gaps)
        refused = sorted(c for c in offer
                         if targets[c][1] <= acct.spot_size(insts[(c, "spot")].token) + 1e-12)
        should = sorted(c for c in offer if abs(gaps.get(c) or 0.0) > safety.PEG_BLOCK)
        detail = {"gaps": gaps, "offered_spot_targets": {c: v[1] for c, v in offer.items()},
                  "allowed_spot_targets": {c: v[1] for c, v in targets.items()}, "refused": refused,
                  "gap_above_1pct": should, "notes": notes,
                  "coins_without_spot": sorted({c for c, _m in insts} - set(offer))}
        return self.record(7, "Unit peg guard refuses carry entries on testnet's prices", refused == should and
                           bool(offer), detail)

    # ---- the whole test

    def run(self):
        from .bot import halted, trading_lock

        if halted(self.cfg):
            raise SelftestRefused(f"HALT file present ({self.cfg.halt_path}); `resume` first")
        with trading_lock(self.cfg) as got:
            if not got:
                raise SelftestRefused("another trading process holds the lock")
            before = self.venue.account()
            self.record(0, "start", True, {"master": self.venue.master, "agent": self.venue.agent_address,
                                           "network": self.venue.market.network, "account": _account_brief(before),
                                           "agent_record": self.venue.agent_validity()})
            safety.mark_holdings_pending(self.store, "selftest")
            self.positions_before = {c: p.size for c, p in before.positions.items()}
            steps = [(5, self.forbidden_moves), (6, self.records_and_export), (7, self.peg_guard)]
            if self.balance_mode():  # trading only in a mode the bot supports
                steps = [(2, self.usdc_round_trip), (3, self.spot_through_batch), (4, self.perp_open_close)] + steps
            for number, step in steps:
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 - one broken step must not hide the others
                    log.exception("selftest step %s", number)
                    self.record(number, step.__name__, False, {"error": f"{type(exc).__name__}: {exc}"})
            after = self.venue.account()
            safety.snapshot_holdings(self.store, self.venue, after, "after selftest")
            cost = before.usdc_equity - after.usdc_equity
            # Positions that were there before the selftest are not its: it must leave every coin as it found it.
            now_pos = {c: p.size for c, p in after.positions.items()}
            changed = {c: {"before": self.positions_before.get(c, 0.0), "after": now_pos.get(c, 0.0)}
                       for c in set(self.positions_before) | set(now_pos)
                       if not math.isclose(self.positions_before.get(c, 0.0), now_pos.get(c, 0.0),
                                           rel_tol=0.0, abs_tol=1e-9)}
            self.record(8, "end", not changed, {"account": _account_brief(after),
                                                "positions_before": self.positions_before,
                                                "positions_changed": changed,
                                                "usdc_spent_on_fees_and_spreads": round(cost, 6),
                                                "includes_drift_of_positions_held_before": bool(self.positions_before)})
        return {"session": self.session, "network": self.venue.market.network,
                "ok": all(s["ok"] for s in self.steps), "steps": self.steps}


def run(cfg, venue, store, spot_choice=None, sleep=time.sleep, clock=time.time):
    return Selftest(cfg, venue, store, sleep=sleep, clock=clock, spot_choice=spot_choice).run()
