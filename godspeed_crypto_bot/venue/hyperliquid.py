"""Hyperliquid: public market data (no key) and the signing account (agent key, live and testnet only).

Uses the official SDK, hyperliquid-python-sdk 0.24.0 (github.com/hyperliquid-dex/hyperliquid-python-sdk,
MIT), pinned exactly in pyproject.toml. Two actions are not in that SDK version and are built here from the
API documentation with the SDK's own L1 signing: agentSendAsset (moving USDC between the spot and perp
balances of the same account, which an agent key may sign; usdClassTransfer is user-signed and needs the
master key) and the userAbstraction query.

The agent (API) wallet signs orders for the master account (account_address). It cannot withdraw or send
funds out of it: withdraw3 and usdSend are user-signed actions, which Hyperliquid attributes to whoever signed
them, so signed by the agent they act for the agent's own empty address (testnet, 2026-10-17: "Must deposit
before performing actions. User: <agent>"); agentSendAsset only moves funds within the same address.

Balance modes: a unified account (one USDC balance, reported in the spot state) or separate spot and perp
balances ('disabled'). The bot reads the mode and never sets it.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import eth_account
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.error import ClientError, ServerError
from hyperliquid.utils.signing import get_timestamp_ms, sign_l1_action
from hyperliquid.utils.types import Cloid

from ..config import API_URLS
from .base import AccountState, Candle, Instrument, MarketData, OrderResult, Position, Venue

log = logging.getLogger(__name__)
DAY_MS = 86_400_000
PERP_PX_DECIMALS = 6
SPOT_PX_DECIMALS = 8
# The balance modes the bot runs in: one unified USDC balance, or separate spot and perp balances ("manual" /
# "standard"). Portfolio margin (borrowing against other assets) and the discontinued DEX abstraction are not.
SUPPORTED_MODES = ("unifiedAccount", "disabled")


def _ms(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


class HyperliquidMarketData(MarketData):
    RETRIES = []  # every retried info call in this process (one command per process), for the invocation record

    def __init__(self, network="mainnet", spot_tokens=None, coins=("BTC", "ETH", "SOL"), info=None, timeout=15):
        self.network = network
        self.base_url = API_URLS[network]
        self.spot_tokens = dict(spot_tokens or {"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"})
        self.coins = tuple(coins)
        self.info = info or Info(self.base_url, skip_ws=True, timeout=timeout)
        self._instruments = None
        self._meta = None
        self._spot_meta = None
        self.usdc_token = None  # "USDC:0x..." for agentSendAsset

    def open_holdings(self, address):
        """What an account holds that the strategy trades, from public data (no key): {"perp:BTC": size, ...}.

        Used before an uninstall, which must not leave someone unaware of positions that stay open."""
        out = {}
        st = self._retry(self.info.user_state, address)
        for ap in st.get("assetPositions", []):
            p = ap["position"]
            if float(p["szi"]) != 0:
                out[f"perp:{p['coin']}"] = float(p["szi"])
        sp = self._retry(self.info.spot_user_state, address)
        tokens = set(self.spot_tokens.values())
        for b in sp.get("balances", []):
            if b["coin"] in tokens and float(b["total"]) != 0:
                out[f"spot:{b['coin']}"] = float(b["total"])
        return out

    def _retry(self, fn, *args, attempts=4):
        delay = 1.0
        for k in range(attempts):
            try:
                return fn(*args)
            except (requests.RequestException, ServerError) as exc:
                if k == attempts - 1:
                    raise
                log.warning("Hyperliquid info call failed (%s); retrying in %.0fs", exc, delay)
                HyperliquidMarketData.RETRIES.append({"at": time.time(), "call": getattr(fn, "__name__", str(fn)),
                                                      "attempt": k + 1, "error": f"{type(exc).__name__}: {exc}"})
                time.sleep(delay)
                delay *= 2

    def meta(self):
        if self._meta is None:
            self._meta = self._retry(self.info.meta)
        return self._meta

    def listed_perps(self):
        """The perpetuals Hyperliquid lists now (its main exchange, delisted ones left out): what a strategy file's
        coins are checked against."""
        return {u["name"] for u in self.meta()["universe"] if not u.get("isDelisted")}

    def spot_meta(self):
        if self._spot_meta is None:
            self._spot_meta = self._retry(self.info.spot_meta)
        return self._spot_meta

    def instruments(self):
        if self._instruments is not None:
            return self._instruments
        out = {}
        perps = {u["name"]: u for u in self.meta()["universe"]}
        for coin in self.coins:
            u = perps.get(coin)
            if u is None or u.get("isDelisted"):
                continue
            out[(coin, "perp")] = Instrument(coin, "perp", coin, int(u["szDecimals"]),
                                             PERP_PX_DECIMALS - int(u["szDecimals"]), None, int(u["maxLeverage"]))
        sm = self.spot_meta()
        tokens = {t["name"]: t for t in sm["tokens"]}
        usdc = tokens.get("USDC")
        if usdc is None:
            raise RuntimeError("Hyperliquid spot metadata has no USDC token")
        self.usdc_token = f"USDC:{usdc['tokenId']}"
        for coin, token_name in self.spot_tokens.items():
            t = tokens.get(token_name)
            if t is None or coin not in self.coins:
                continue
            pairs = [u for u in sm["universe"] if u["tokens"] == [t["index"], usdc["index"]]]
            if not pairs:
                continue
            pair = pairs[0]
            out[(coin, "spot")] = Instrument(coin, "spot", pair["name"], int(t["szDecimals"]),
                                             SPOT_PX_DECIMALS - int(t["szDecimals"]), token_name)
        self._instruments = out
        return out

    def daily_candles(self, inst, start, end):
        now = int(time.time() * 1000)
        rows = self._retry(self.info.candles_snapshot, inst.name, "1d", _ms(start), _ms(end + timedelta(days=1)) - 1)
        out = []
        for r in rows:
            d = _day(int(r["t"]))
            if start <= d <= end:
                out.append(Candle(d, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"]),
                                  int(r["n"]), int(r["T"]) < now))
        return sorted(out, key=lambda c: c.day)

    def funding_history(self, coin, start_ms, end_ms):
        out, cursor = [], start_ms
        while cursor < end_ms:
            rows = self._retry(self.info.funding_history, coin, cursor, end_ms)
            if not rows:
                break
            out.extend((int(r["time"]), float(r["fundingRate"])) for r in rows)
            last = int(rows[-1]["time"])
            if len(rows) < 500 or last + 1 <= cursor:
                break
            cursor = last + 1
        seen, unique = set(), []
        for t, r in sorted(out):
            if t not in seen:
                seen.add(t)
                unique.append((t, r))
        return unique

    def mids(self):
        return {k: float(v) for k, v in self._retry(self.info.all_mids).items()}

    def perp_marks(self):
        meta, ctxs = self._retry(self.info.meta_and_asset_ctxs)
        return {u["name"]: float(c["markPx"]) for u, c in zip(meta["universe"], ctxs, strict=False)
                if u["name"] in self.coins and c.get("markPx") is not None}

    def l2_book(self, inst):
        snap = self._retry(self.info.l2_snapshot, inst.name)
        bids = [(float(x["px"]), float(x["sz"])) for x in snap["levels"][0]]
        asks = [(float(x["px"]), float(x["sz"])) for x in snap["levels"][1]]
        return bids, asks

    def user_abstraction(self, user):
        return self._retry(self.info.post, "/info", {"type": "userAbstraction", "user": user})

    def extra_agents(self, user):
        return self._retry(self.info.extra_agents, user)


def parse_order_response(resp, requested):
    """One IOC order's outcome from Hyperliquid's /exchange response."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return OrderResult("rejected", requested, error=str(resp), raw=resp)
    statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return OrderResult("error", requested, error="no order status in response", raw=resp)
    s = statuses[0]
    if "filled" in s:
        f = s["filled"]
        filled = float(f["totalSz"])
        status = "filled" if filled >= requested - 1e-12 else "partial"
        return OrderResult(status, requested, filled, float(f["avgPx"]), None, int(f["oid"]), raw=resp)
    if "error" in s:
        msg = s["error"]
        status = "unfilled" if "could not immediately match" in msg else "rejected"
        return OrderResult(status, requested, error=msg, raw=resp)
    if "resting" in s:
        return OrderResult("error", requested, oid=int(s["resting"]["oid"]), error="IOC order rested", raw=resp)
    return OrderResult("error", requested, error=f"unknown status {s}", raw=resp)


class HyperliquidVenue(Venue):
    """The master account, traded by the agent key. Constructed only in live mode (mainnet or testnet)."""

    signs = True

    def __init__(self, market: HyperliquidMarketData, master_address: str, agent_key: str, exchange=None,
                 allow_missing_spot=False):
        super().__init__(market)
        self.master = master_address
        self.wallet = eth_account.Account.from_key(agent_key)
        self.is_mainnet = market.network == "mainnet"
        if allow_missing_spot and self.is_mainnet:
            raise ValueError("allow_missing_spot is a testnet-only setting")
        self.allow_missing_spot = bool(allow_missing_spot)
        self.preflight_notes = []
        market.instruments()
        self.exchange = exchange or Exchange(
            self.wallet, market.base_url, meta=market.meta(), account_address=master_address,
            spot_meta=market.spot_meta(), timeout=15,
        )

    @property
    def agent_address(self):
        return self.wallet.address

    def mode(self):
        m = self.market.user_abstraction(self.master)
        return m if isinstance(m, str) else str(m)

    def account(self):
        mode = self.mode()
        st = self.market._retry(self.market.info.user_state, self.master)
        sp = self.market._retry(self.market.info.spot_user_state, self.master)
        positions = {}
        for ap in st.get("assetPositions", []):
            p = ap["position"]
            size = float(p["szi"])
            if size == 0:
                continue
            positions[p["coin"]] = Position(
                p["coin"], size, float(p["entryPx"]) if p.get("entryPx") else None, float(p["unrealizedPnl"]),
                float(p["marginUsed"]), float(p["liquidationPx"]) if p.get("liquidationPx") else None,
                float(p["positionValue"]) * (1 if size > 0 else -1),
            )
        spot = {b["coin"]: (float(b["total"]), float(b["hold"])) for b in sp.get("balances", [])}
        ms = st["marginSummary"]
        maint = float(st.get("crossMaintenanceMarginUsed", 0.0))
        if mode == "unifiedAccount":
            # Measured on testnet 2026-10-17: the USDC is in the spot state (total without unrealized PnL; its hold
            # is what the perps reserve); the perp state still lists positions and margin, but its accountValue is
            # only the slice reserved for them. The collateral is the USDC balance plus the unrealized PnL.
            usdc = spot.get("USDC", (0.0, 0.0))[0]
            upnl = sum(p.unrealized_pnl for p in positions.values())
            available = dict((int(t), float(a)) for t, a in sp.get("tokenToAvailableAfterMaintenance", []))
            usdc_index = self._usdc_index()
            free = available.get(usdc_index, usdc - spot.get("USDC", (0.0, 0.0))[1])
            return AccountState(usdc + upnl, free, maint, float(ms["totalMarginUsed"]), positions, spot, mode)
        return AccountState(float(ms["accountValue"]), float(st["withdrawable"]), maint, float(ms["totalMarginUsed"]),
                            positions, spot, mode)

    def _usdc_index(self):
        return next((int(t["index"]) for t in self.market.spot_meta()["tokens"] if t["name"] == "USDC"), 0)

    def place_ioc(self, inst, is_buy, size, limit_px, cloid, reduce_only=False):
        order_type = {"limit": {"tif": "Ioc"}}
        c = Cloid.from_str(cloid)
        try:
            resp = self.exchange.order(inst.name, is_buy, size, limit_px, order_type, reduce_only=reduce_only, cloid=c)
        except (requests.RequestException, ServerError) as exc:
            # The order may or may not have reached the book. Ask by client order id before anyone retries.
            log.warning("order %s: %s; checking its status by cloid", cloid, exc)
            return self._status_by_cloid(c, size, str(exc))
        except ClientError as exc:
            return OrderResult("rejected", size, error=f"{exc.error_code}: {exc.error_message}")
        result = parse_order_response(resp, size)
        if result.status == "error" and result.oid is not None:  # an IOC must never rest; cancel defensively
            try:
                self.exchange.cancel(inst.name, result.oid)
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                result.error += f"; cancel failed: {exc}"
        return result

    def place_ioc_batch(self, orders):
        """Several IOC orders, one signed action per asset class, sent back to back: spot first, then perp.

        Hyperliquid refuses spot and perp orders in one order action ("Order action has different asset
        classes.", testnet 2026-10-17), so a carry pair's two legs are two actions, the thinner spot book first.
        orders: [(inst, is_buy, size, limit_px, cloid, reduce_only)]. One result per order, in order.
        """
        out = [None] * len(orders)
        self.last_batch_actions = []
        for market in ("spot", "perp"):
            idx = [k for k, o in enumerate(orders) if o[0].market == market]
            if not idx:
                continue
            self.last_batch_actions.append({"market": market, "orders": len(idx)})
            for k, res in zip(idx, self._one_action([orders[k] for k in idx]), strict=True):
                out[k] = res
        return out

    def _one_action(self, orders):
        """IOC orders of ONE asset class in one signed order action."""
        reqs = [
            {"coin": inst.name, "is_buy": is_buy, "sz": size, "limit_px": limit_px,
             "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": reduce_only, "cloid": Cloid.from_str(cloid)}
            for inst, is_buy, size, limit_px, cloid, reduce_only in orders
        ]
        try:
            resp = self.exchange.bulk_orders(reqs)
        except (requests.RequestException, ServerError) as exc:
            log.warning("batch order failed in transit (%s); checking each order by cloid", exc)
            return [self._status_by_cloid(r["cloid"], r["sz"], str(exc)) for r in reqs]
        except ClientError as exc:
            return [OrderResult("rejected", r["sz"], error=f"{exc.error_code}: {exc.error_message}") for r in reqs]
        if not (isinstance(resp, dict) and resp.get("status") == "ok"):
            return [OrderResult("rejected", r["sz"], error=str(resp), raw=resp) for r in reqs]
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        out = []
        for k, r in enumerate(reqs):
            one = {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses[k:k + 1]}}}
            res = parse_order_response(one, r["sz"])
            if res.status == "error" and res.oid is not None:
                try:
                    self.exchange.cancel(r["coin"], res.oid)
                except Exception as exc:  # noqa: BLE001
                    res.error += f"; cancel failed: {exc}"
            out.append(res)
        return out

    def _status_by_cloid(self, cloid, size, why):
        try:
            q = self.market._retry(self.market.info.query_order_by_cloid, self.master, cloid)
        except Exception as exc:  # noqa: BLE001
            return OrderResult("error", size, error=f"{why}; status unknown: {exc}")
        order = (q or {}).get("order") if isinstance(q, dict) else None
        if not order:
            return OrderResult("unfilled", size, error=f"{why}; no such order on the exchange")
        o = order.get("order", {})
        orig, left = float(o.get("origSz", size)), float(o.get("sz", 0.0))
        filled = max(0.0, orig - left)
        status = "filled" if left == 0 else ("partial" if filled else "unfilled")
        return OrderResult(status, size, filled, None, None, o.get("oid"), error=None if filled else why, raw=q)

    def transfer_usdc(self, amount, to_perp):
        """Move USDC between this account's spot and perp balances (agentSendAsset, same address only)."""
        if amount <= 0:
            return True
        resp = self.send_usdc_between_balances(amount, to_perp)
        ok = isinstance(resp, dict) and resp.get("status") == "ok"
        if not ok:
            log.warning("USDC transfer refused: %s", resp)
        return ok

    def send_usdc_between_balances(self, amount, to_perp):
        """The agentSendAsset action itself; returns Hyperliquid's response as it came."""
        ts = get_timestamp_ms()
        action = {
            "type": "agentSendAsset",
            "destination": self.master.lower(),
            "sourceDex": "spot" if to_perp else "",
            "destinationDex": "" if to_perp else "spot",
            "token": self.market.usdc_token,
            "amount": f"{amount:.6f}".rstrip("0").rstrip("."),
            "fromSubAccount": "",
            "nonce": ts,
        }
        sig = sign_l1_action(self.wallet, action, None, ts, None, self.is_mainnet)
        return self.exchange._post_action(action, sig, ts)

    def set_leverage(self, coin, leverage):
        resp = self.exchange.update_leverage(int(leverage), coin, is_cross=True)
        if not (isinstance(resp, dict) and resp.get("status") == "ok"):
            raise RuntimeError(f"setting {coin} leverage to {leverage}x cross was refused: {resp}")

    def agent_validity(self):
        """The agent as Hyperliquid lists it for the master: validUntil is in the agent's record."""
        agents = self.market.extra_agents(self.master) or []
        me = self.agent_address.lower()
        for a in agents:
            if isinstance(a, dict) and str(a.get("address", "")).lower() == me:
                vu = a.get("validUntil")
                return {"address": self.agent_address, "name": a.get("name"), "listed": True,
                        "valid_until_ms": int(vu) if vu else None}
        return {"address": self.agent_address, "listed": False, "valid_until_ms": None}

    def ledger_updates(self, start_ms, end_ms):
        return self.market._retry(self.market.info.user_non_funding_ledger_updates, self.master, start_ms,
                                  end_ms) or []

    def sync_records(self, store, start_ms, end_ms):
        """Fills (hash, oid, tid, fee), funding (hash) and ledger updates (hash) from Hyperliquid itself."""
        by_name = {i.name: i for i in self.market.instruments().values()}
        n_fill = 0
        for f in self.fills(start_ms, end_ms):
            inst = by_name.get(f["coin"])
            coin = inst.coin if inst else f["coin"]
            market = inst.market if inst else ("spot" if f["coin"].startswith("@") or "/" in f["coin"] else "perp")
            store.add_venue_fill(int(f["time"]), coin, market, f["coin"], "buy" if f["side"] == "B" else "sell",
                                 float(f["px"]), float(f["sz"]), float(f.get("fee", 0) or 0), f.get("feeToken"),
                                 float(f.get("closedPnl", 0) or 0), f.get("dir"), f.get("hash"), f.get("oid"),
                                 f.get("tid"), f.get("cloid"), f)
            n_fill += 1
        n_fund = 0
        for p in self.funding_payments(start_ms, end_ms):
            store.add_funding(p["time"], p["coin"], p["usdc"], p["szi"], p["rate"], "userFunding", p.get("hash"))
            n_fund += 1
        n_ledger = 0
        for r in self.ledger_updates(start_ms, end_ms):
            d = r.get("delta", {})
            kind = d.get("type", "unknown")
            usdc = d.get("usdc", d.get("usdcValue"))
            direction = None
            if kind == "accountClassTransfer":
                direction = "spot->perp" if d.get("toPerp") else "perp->spot"
            elif kind in ("send", "spotTransfer", "internalTransfer", "subAccountTransfer"):
                direction = f"{d.get('user', '')}->{d.get('destination', '')}"
                if d.get("sourceDex") is not None:
                    direction += f" ({d.get('sourceDex') or 'perp'}->{d.get('destinationDex') or 'perp'})"
            ref = f"{r.get('hash')}:{kind}:{r.get('time')}"
            store.add_venue_ledger(int(r["time"]), kind, float(usdc) if usdc is not None else None, ref,
                                   r.get("hash"), d.get("token"),
                                   float(d["amount"]) if d.get("amount") is not None else None, direction, r)
            n_ledger += 1
        return {"fills": n_fill, "funding": n_fund, "ledger": n_ledger}

    def funding_payments(self, start_ms, end_ms):
        rows = self.market._retry(self.market.info.user_funding_history, self.master, start_ms, end_ms) or []
        out = []
        for r in rows:
            d = r.get("delta", {})
            if d.get("type") != "funding":
                continue
            out.append({"time": int(r["time"]), "coin": d["coin"], "usdc": float(d["usdc"]), "szi": float(d["szi"]),
                        "rate": float(d["fundingRate"]), "hash": r.get("hash")})
        return out

    def fills(self, start_ms, end_ms):
        return self.market._retry(self.market.info.user_fills_by_time, self.master, start_ms, end_ms) or []

    def external_flows(self, start_ms):
        rows = self.market._retry(self.market.info.user_non_funding_ledger_updates, self.master, start_ms) or []
        me = self.master.lower()
        flow = 0.0
        for r in rows:
            d = r.get("delta", {})
            kind = d.get("type")
            if kind == "deposit":
                flow += float(d.get("usdc", 0))
            elif kind == "withdraw":
                flow -= float(d.get("usdc", 0)) + float(d.get("fee", 0))
            elif kind in ("internalTransfer", "spotTransfer", "send", "subAccountTransfer"):
                src = str(d.get("user", "")).lower()
                dst = str(d.get("destination", "")).lower()
                usd = float(d.get("usdcValue", d.get("usdc", d.get("amount", 0))) or 0)
                if src == dst:
                    continue  # within this account (e.g. spot <-> perp)
                if dst == me:
                    flow += usd
                elif src == me:
                    flow -= usd + float(d.get("fee", 0) or 0)
        return flow

    def preflight(self):
        problems = []
        mode = self.mode()
        if mode not in SUPPORTED_MODES:
            problems.append(
                f"the account's balance mode is {mode!r}; the bot runs on a unified account ('unifiedAccount') or "
                "on separate spot and perp balances ('disabled'), not on portfolio margin, DEX abstraction or an "
                "account that never chose a mode ('default')"
            )
        try:
            agents = self.market.extra_agents(self.master) or []
            addresses = {str(a.get("address", "")).lower() for a in agents if isinstance(a, dict)}
            if self.agent_address.lower() not in addresses:
                problems.append(f"agent {self.agent_address} is not approved for {self.master} on {self.market.network}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not list approved agents: {exc}")
        missing = [k for k in ((c, m) for c in ("BTC", "ETH", "SOL") for m in ("perp", "spot"))
                   if k not in self.market.instruments()]
        if self.allow_missing_spot:  # testnet only (refused on mainnet): a coin without spot is not carried
            spotless = [c for c, m in missing if m == "spot"]
            missing = [k for k in missing if k[1] != "spot"]
            self.preflight_notes = [f"no spot token for {c} on {self.market.network}: {c} is not carried "
                                    "(allow_missing_spot)" for c in spotless]
        if missing:
            problems.append(f"instruments missing on {self.market.network}: {missing}")
        return problems


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
