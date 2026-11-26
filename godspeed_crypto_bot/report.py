"""The comparison: the account (live, paper or dry-run) against the backtest's assumptions on the same days.

The shadow ledger is the backtest on those days: fills at the daily open with the backtest's fees and
slippage tiers, funding as the daily rate times the close. The account is what really happened (or, in
paper, what the order book and the actual hourly funding would have given). The report puts the two side
by side and splits the difference into the parts the backtest assumed: fills (slippage against the
decision mid), fees, funding, and orders that never happened (skipped as too small, failed, halted).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .ledger import MODEL_SLIPPAGE, PERP_FEE, SPOT_FEE
from .names import DISPLAY_NAME
from .strategy import STRATEGY_WORDS

FEE = {"perp": PERP_FEE, "spot": SPOT_FEE}


def _series(store, book):
    return {r["day"]: (r["equity"], json.loads(r["detail"] or "{}")) for r in store.equity_series(book)}


def compare(store, start: date | None = None, end: date | None = None):
    shadow = _series(store, "shadow_total")
    trend = _series(store, "shadow_trend")
    carry = _series(store, "shadow_carry")
    account = _series(store, "account")
    days = sorted(d for d in shadow if d in account)
    if start:
        days = [d for d in days if d >= str(start)]
    if end:
        days = [d for d in days if d <= str(end)]
    out = {"days": len(days), "first_day": days[0] if days else None, "last_day": days[-1] if days else None}
    if len(days) >= 1:
        s0, s1 = shadow[days[0]][0], shadow[days[-1]][0]
        a0, a1 = account[days[0]][0], account[days[-1]][0]
        out["shadow_return"] = s1 / s0 - 1 if s0 else None
        out["account_return"] = a1 / a0 - 1 if a0 else None
        out["difference"] = (out["account_return"] or 0) - (out["shadow_return"] or 0)

        def delta(series, key):
            if days[0] not in series or days[-1] not in series:
                return None
            return series[days[-1]][1].get(key, 0.0) - series[days[0]][1].get(key, 0.0)

        out["shadow"] = {
            book: {k: delta(series, k) for k in ("price_pnl", "funding", "fees", "slippage")}
            for book, series in (("trend", trend), ("carry", carry))
        }
    lo, hi = days[0] if days else "0000", days[-1] if days else "9999"
    rows = store.db.execute(
        "SELECT market, coin, filled, avg_px, decision_mid, fee, slippage_bps, status FROM orders "
        "WHERE mode=? AND network=? AND day>? AND day<=? AND filled>0",
        (store.mode, store.network, lo, hi),
    ).fetchall()
    fills = {}
    for r in rows:
        f = fills.setdefault(r["market"], {"orders": 0, "notional": 0.0, "fees": 0.0, "model_fees": 0.0,
                                           "slippage_cost": 0.0, "model_slippage_cost": 0.0})
        notional = r["filled"] * (r["avg_px"] or r["decision_mid"] or 0)
        f["orders"] += 1
        f["notional"] += notional
        f["fees"] += r["fee"] or 0.0
        f["model_fees"] += notional * FEE[r["market"]]
        f["slippage_cost"] += notional * (r["slippage_bps"] or 0.0) / 1e4
        f["model_slippage_cost"] += notional * MODEL_SLIPPAGE.get(r["coin"], 15e-4)
    for f in fills.values():
        f["slippage_bps"] = f["slippage_cost"] / f["notional"] * 1e4 if f["notional"] else None
        f["model_slippage_bps"] = f["model_slippage_cost"] / f["notional"] * 1e4 if f["notional"] else None
    out["fills"] = fills
    t0 = int(datetime.fromisoformat(lo).replace(tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000 if days else 0
    t1 = int(datetime.fromisoformat(hi).replace(tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000 if days else 0
    fr = store.db.execute(
        "SELECT source, SUM(usdc) AS usdc, COUNT(*) AS n FROM funding WHERE mode=? AND network=? AND time_ms>? "
        "AND time_ms<=? GROUP BY source", (store.mode, store.network, t0, t1)).fetchall()
    out["funding_actual"] = {r["source"]: {"usdc": r["usdc"], "payments": r["n"]} for r in fr}
    sk = store.db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(notional), 0) AS usd FROM skipped_orders WHERE mode=? AND network=? "
        "AND day>? AND day<=?", (store.mode, store.network, lo, hi)).fetchone()
    out["skipped_orders"] = {"count": sk["n"], "notional": sk["usd"]}
    bad = store.db.execute(
        "SELECT status, COUNT(*) AS n FROM orders WHERE mode=? AND network=? AND day>? AND day<=? AND status IN "
        "('failed', 'failed_leg', 'unwound', 'unfilled', 'stopped_day_cap') GROUP BY status",
        (store.mode, store.network, lo, hi)).fetchall()
    out["not_done"] = {r["status"]: r["n"] for r in bad}
    ga = store.db.execute(
        "SELECT action, COUNT(*) AS n FROM guard_actions WHERE mode=? AND network=? AND ts>=? GROUP BY action",
        (store.mode, store.network, t0 / 1000 - 86400)).fetchall()
    out["guard_actions"] = {r["action"]: r["n"] for r in ga}
    return out


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _usd(x):
    return "n/a" if x is None else f"{x:,.2f}"


def markdown(cfg, doc_week, doc_all, today):
    lines = [
        f"# {DISPLAY_NAME}: week to {today}",
        "",
        f"Mode {cfg.mode}, network {cfg.network}, strategy: {STRATEGY_WORDS[cfg.strategy]}, capital cap {cfg.capital_cap_usdc:,.0f} USDC.",
        "The backtest column is the shadow ledger: the backtest's assumptions (fill at the daily open, its fees",
        "and slippage tiers, daily funding times the close) applied to the same days and the same decisions.",
        "",
    ]
    for title, doc in (("Last 7 days", doc_week), ("Since the start", doc_all)):
        lines += [f"## {title}", ""]
        if not doc["days"]:
            lines += ["No days recorded yet.", ""]
            continue
        lines += [
            f"Days {doc['first_day']} to {doc['last_day']} ({doc['days']} closes).",
            "",
            f"- Account return: {_pct(doc.get('account_return'))}",
            f"- Backtest assumptions on the same days: {_pct(doc.get('shadow_return'))}",
            f"- Difference: {_pct(doc.get('difference'))}",
        ]
        for market, f in sorted(doc["fills"].items()):
            lines.append(
                f"- {market} fills: {f['orders']} orders, {_usd(f['notional'])} USDC traded; slippage against the "
                f"decision mid {f['slippage_bps'] if f['slippage_bps'] is None else round(f['slippage_bps'], 2)} bps "
                f"(backtest assumed {round(f['model_slippage_bps'], 2) if f['model_slippage_bps'] else 'n/a'} bps); "
                f"fees {_usd(f['fees'])} (backtest {_usd(f['model_fees'])})"
            )
        for src, fa in doc["funding_actual"].items():
            lines.append(f"- Funding booked ({src}): {_usd(fa['usdc'])} USDC over {fa['payments']} payments")
        sh = doc.get("shadow", {})
        if sh:
            lines.append(
                "- Backtest funding: trend " + _usd(sh["trend"]["funding"]) + ", carry " + _usd(sh["carry"]["funding"])
                + " USDC; backtest fees and slippage: " + _usd(
                    sum((sh[b]["fees"] or 0) + (sh[b]["slippage"] or 0) for b in ("trend", "carry"))) + " USDC"
            )
        lines.append(f"- Orders skipped as too small: {doc['skipped_orders']['count']} "
                     f"({_usd(doc['skipped_orders']['notional'])} USDC)")
        if doc["not_done"]:
            lines.append(f"- Orders not done: {doc['not_done']}")
        if doc["guard_actions"]:
            lines.append(f"- Guard actions: {doc['guard_actions']}")
        lines.append("")
    return "\n".join(lines)


def weekly(cfg, store, today: date | None = None, out_dir: Path | None = None):
    today = today or datetime.now(timezone.utc).date()
    week = compare(store, today - timedelta(days=7), today)
    whole = compare(store)
    text = markdown(cfg, week, whole, today)
    out_dir = Path(out_dir or cfg.reports_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly-{cfg.mode}-{cfg.network}-{today}.md"
    path.write_text(text, encoding="utf-8")
    (out_dir / f"weekly-{cfg.mode}-{cfg.network}-{today}.json").write_text(
        json.dumps({"week": week, "all": whole}, indent=1, default=str), encoding="utf-8")
    return path, text
