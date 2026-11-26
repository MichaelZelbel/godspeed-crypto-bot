"""Tax-ready export: every fill, fee, funding payment and USDC transfer as one CSV row.

Timestamps are UTC (ISO 8601, "Z"). The EUR columns are left blank on purpose: the conversion is done by
whoever prepares the tax return, with the rate source they choose. Each row carries the venue's own
identifiers (Hyperliquid transaction hash, order id, trade id) where the venue gives them; simulated rows
(dry-run, paper) carry "sim-" ids and no hash, so they can never be mistaken for real ones.

Fees get their own rows (category "fee", linked to the trade by its trade id) so a sum per category
is each category's total; the trade row repeats the fee for reference only.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

COLUMNS = [
    "timestamp_utc", "mode", "network", "category", "asset", "market", "side", "quantity", "price_usdc",
    "value_usdc", "fee", "fee_asset", "realized_pnl_usdc", "cash_effect_usdc", "eur_rate", "eur_value",
    "venue_tx_hash", "venue_order_id", "venue_trade_id", "client_order_id", "description",
]


def _ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _bounds(start: date | None, end: date | None):
    lo = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000) if start else 0
    hi = (int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp() * 1000)
          + int(timedelta(days=1).total_seconds() * 1000)) if end else 2**62
    return lo, hi


def rows(store, start=None, end=None):
    lo, hi = _bounds(start, end)
    base = {"mode": store.mode, "network": store.network, "eur_rate": "", "eur_value": ""}
    out = []
    q = ("SELECT * FROM venue_fills WHERE mode=? AND network=? AND time_ms>=? AND time_ms<? ORDER BY time_ms, id")
    for f in store.db.execute(q, (store.mode, store.network, lo, hi)):
        value = f["px"] * f["sz"]
        if f["market"] == "spot":
            cash = -value if f["side"] == "buy" else value
            asset = f["venue_coin"] if f["venue_coin"] and not f["venue_coin"].startswith("@") else f["coin"]
        else:
            cash = f["closed_pnl"] or 0.0
            asset = f["coin"]
        desc = f"{f['dir'] or ''} {f['coin']} {f['market']}".strip()
        out.append({**base, "timestamp_utc": _ts(f["time_ms"]), "category": "trade", "asset": asset,
                    "market": f["market"], "side": f["side"], "quantity": f["sz"], "price_usdc": f["px"],
                    "value_usdc": value, "fee": f["fee"], "fee_asset": f["fee_token"],
                    "realized_pnl_usdc": f["closed_pnl"] if f["market"] == "perp" else "",
                    "cash_effect_usdc": cash, "venue_tx_hash": f["hash"] or "", "venue_order_id": f["oid"] or "",
                    "venue_trade_id": f["tid"], "client_order_id": f["cloid"] or "", "description": desc})
        if f["fee"]:
            fee_usdc = f["fee"] if (f["fee_token"] or "USDC") == "USDC" else ""
            out.append({**base, "timestamp_utc": _ts(f["time_ms"]), "category": "fee", "asset": f["fee_token"] or "USDC",
                        "market": f["market"], "side": "", "quantity": f["fee"], "price_usdc": "",
                        "value_usdc": fee_usdc, "fee": "", "fee_asset": "", "realized_pnl_usdc": "",
                        "cash_effect_usdc": -fee_usdc if fee_usdc != "" else "",
                        "venue_tx_hash": f["hash"] or "", "venue_order_id": f["oid"] or "",
                        "venue_trade_id": f["tid"], "client_order_id": f["cloid"] or "",
                        "description": f"trading fee for trade {f['tid']}"})
    q = "SELECT * FROM funding WHERE mode=? AND network=? AND time_ms>=? AND time_ms<? ORDER BY time_ms, id"
    for r in store.db.execute(q, (store.mode, store.network, lo, hi)):
        out.append({**base, "timestamp_utc": _ts(r["time_ms"]), "category": "funding", "asset": r["coin"],
                    "market": "perp", "side": "received" if r["usdc"] >= 0 else "paid", "quantity": r["szi"],
                    "price_usdc": "", "value_usdc": abs(r["usdc"]), "fee": "", "fee_asset": "",
                    "realized_pnl_usdc": "", "cash_effect_usdc": r["usdc"], "venue_tx_hash": r["hash"] or "",
                    "venue_order_id": "", "venue_trade_id": "", "client_order_id": "",
                    "description": f"funding at rate {r['rate']} ({r['source']})"})
    q = "SELECT * FROM venue_ledger WHERE mode=? AND network=? AND time_ms>=? AND time_ms<? ORDER BY time_ms, id"
    for r in store.db.execute(q, (store.mode, store.network, lo, hi)):
        kind = r["kind"]
        if kind in ("deposit", "simulated_deposit"):
            category, cash = "deposit", r["usdc"]
        elif kind == "withdraw":
            category, cash = "withdrawal", -(r["usdc"] or 0.0)
        else:
            category, cash = "transfer", 0.0 if kind == "accountClassTransfer" else ""
        out.append({**base, "timestamp_utc": _ts(r["time_ms"]), "category": category, "asset": r["token"] or "USDC",
                    "market": "", "side": r["direction"] or "", "quantity": r["amount"] if r["amount"] is not None
                    else r["usdc"], "price_usdc": "", "value_usdc": r["usdc"] if r["usdc"] is not None else "",
                    "fee": "", "fee_asset": "", "realized_pnl_usdc": "", "cash_effect_usdc": cash,
                    "venue_tx_hash": r["hash"] or "", "venue_order_id": "", "venue_trade_id": r["ref"],
                    "client_order_id": "", "description": kind})
    out.sort(key=lambda r: (r["timestamp_utc"], r["category"]))
    return out


def to_csv(store, start=None, end=None, fh=None):
    buf = fh or io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    data = rows(store, start, end)
    for r in data:
        w.writerow(r)
    return (buf.getvalue() if fh is None else None), len(data)
