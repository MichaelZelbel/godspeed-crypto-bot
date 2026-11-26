"""Checks that keep the bot from trading on a false picture of the world (from the Hyperliquid venue study).

1. API wallet expiry. Hyperliquid agent wallets expire after at most 180 days. The approval's expiry is
   read daily from the venue (extraAgents validUntil; config fallback: approval date + 180 days) and
   recorded. The bot sends no advance reminders of its own: it keeps one reminder in Godspeed's deadline list
   (outbox.put_deadline, carried by the relay) and Godspeed does the reminding. Preflight silently refuses to
   go live with less than 14 days left. At expiry, or when signing fails because the agent is expired or pruned, the bot
   halts and alerts. It never stops silently.
2. Holdings check (forced deleveraging, one-legged carry). Every daily run and guard run compares what
   the venue holds with what the books, as last executed, say it should hold. A carry pair whose perp
   short shrank while its spot stayed (Hyperliquid's auto-deleveraging closed profitable shorts on
   2025-10-10) is re-hedged if the market allows, else its orphaned spot is sold; either way the owner is
   told. Any other unexplained difference halts trading and alerts.
3. Unit bridge peg. UBTC, UETH and USOL are bridged tokens (2-of-3 MPC). Every guard run compares the
   spot mid with the perp mark: above 1% for 15 minutes, no new carry; above 3% for 15 minutes, the
   coin's carry pairs are closed and the owner is told. Every observation is logged.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from . import godspeed, names, outbox
from .execution import Executor, Leg, is_agent_failure  # noqa: F401 - re-exported
from .venue.base import coins_of

log = logging.getLogger(__name__)
AGENT_MAX_DAYS = 180
EXPIRY_MIN_DAYS_TO_GO_LIVE = 14
PEG_BLOCK = 0.01
PEG_EXIT = 0.03
PEG_WINDOW_S = 15 * 60 - 30  # 15 minutes, minus slack for the five-minute timer's jitter
HEDGE_REL_TOL = 0.005  # spot fees are paid in the received token, so a spot leg runs a few bps short


# ---- 1. API wallet expiry


def agent_expiry(cfg, store, venue, now):
    """Refresh and return the agent record {address, valid_until_ms, source, listed}."""
    rec = store.get("agent") or {}
    info = None
    try:
        info = venue.agent_validity()
    except Exception as exc:  # noqa: BLE001 - a failed read is recorded; the stored expiry still counts
        rec["last_error"] = f"{type(exc).__name__}: {exc}"
    if info is not None:
        if rec.get("address") and info.get("address") and rec["address"] != info["address"]:
            # A renewed key: the old key's expiry is not the new key's. Without a validUntil from the venue the
            # configured fallback (agent_valid_until, agent_approved_on) applies, else the expiry is unknown.
            rec.pop("valid_until_ms", None)
            rec.pop("source", None)
        rec["address"] = info.get("address")
        rec["listed"] = info.get("listed", True)
        if info.get("valid_until_ms"):
            rec["valid_until_ms"] = int(info["valid_until_ms"])
            rec["source"] = "venue (extraAgents validUntil)"
    if not rec.get("valid_until_ms"):
        if cfg.agent_valid_until:
            d = date.fromisoformat(cfg.agent_valid_until)
            rec["valid_until_ms"] = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
            rec["source"] = "config agent_valid_until"
        elif cfg.agent_approved_on:
            d = date.fromisoformat(cfg.agent_approved_on) + timedelta(days=AGENT_MAX_DAYS)
            rec["valid_until_ms"] = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
            rec["source"] = "config agent_approved_on + 180 days"
    if cfg.agent_approved_on:
        rec["approved_on"] = cfg.agent_approved_on
    rec["checked_at"] = now.timestamp()
    store.put("agent", rec)
    request_key_reminder(cfg, rec)
    return rec


def request_key_reminder(cfg, rec):
    """Live on mainnet with a known expiry: ask for the one Godspeed reminder to renew the key (the relay
    creates it, and moves its date when the key is renewed). A failure is logged and changes nothing."""
    if not (cfg.mode == "live" and cfg.network == "mainnet" and rec.get("valid_until_ms")):
        return
    try:
        expires = datetime.fromtimestamp(rec["valid_until_ms"] / 1000, tz=timezone.utc).date()
        outbox.put_deadline(cfg, godspeed.key_deadline(expires, cfg.network))
    except Exception as exc:  # noqa: BLE001 - never stops trading or a halt
        log.warning("key reminder not requested: %s", exc)


def check_agent_expiry(cfg, store, notifier, venue, now, halt):
    """Daily. Records the expiry; halts and alerts only when the agent can no longer sign (expired or gone).

    No advance reminders here: Godspeed's deadline list escalates the renewal (see agent_expiry).
    """
    if not venue.signs:
        return {"status": "not live"}
    rec = agent_expiry(cfg, store, venue, now)
    if rec.get("listed") is False:
        halt("API wallet no longer approved on Hyperliquid (expired or pruned)")
        notifier.alert("agent-missing", "stopped: its Hyperliquid API wallet is no longer approved",
                       f"Hyperliquid no longer lists the bot's API wallet {rec.get('address')} for your account, so "
                       "it cannot sign orders. Trading is halted; positions are untouched.",
                       "Approve a new API wallet for the bot on Hyperliquid (your assistant makes its address with keygen).",
                       window_hours=24)
        return {"status": "halted", "agent": rec}
    vu = rec.get("valid_until_ms")
    if not vu:
        return {"status": "unknown", "agent": rec}
    expiry = datetime.fromtimestamp(vu / 1000, tz=timezone.utc)
    days_left = (expiry - now).total_seconds() / 86400
    out = {"status": "ok", "expires": expiry.isoformat(timespec="minutes"), "days_left": round(days_left, 2)}
    if now >= expiry:
        halt(f"API wallet expired at {expiry:%Y-%m-%d %H:%M} UTC")
        notifier.alert(f"agent-expired:{expiry.date()}", "stopped: its Hyperliquid API wallet expired",
                       f"The API wallet {rec.get('address')} expired on {expiry:%Y-%m-%d %H:%M} UTC. Trading is "
                       "halted; positions are untouched.",
                       "Approve a new API wallet for the bot on Hyperliquid (your assistant makes its address with keygen).",
                       window_hours=24 * 365)
        out["status"] = "expired"
    return out


def expiry_problems(cfg, store, venue, now):
    """For preflight, silently: going live needs a known expiry at least 14 days away."""
    if not venue.signs:
        return []
    rec = agent_expiry(cfg, store, venue, now)
    if rec.get("listed") is False:
        return []  # venue.preflight already says the agent is not approved
    vu = rec.get("valid_until_ms")
    if not vu:
        return [f"the API wallet's expiry is unknown, because Hyperliquid does not report it; tell the bot the day you "
                f"approved the wallet on Hyperliquid with: {names.NAME} configure --agent-approved-on YYYY-MM-DD"]
    expiry = datetime.fromtimestamp(vu / 1000, tz=timezone.utc)
    if expiry - now < timedelta(days=EXPIRY_MIN_DAYS_TO_GO_LIVE):
        return [f"the API wallet expires {expiry:%Y-%m-%d %H:%M} UTC, less than {EXPIRY_MIN_DAYS_TO_GO_LIVE} days "
                "away; approve a new one before going live"]
    return []


# ---- 2. holdings: the venue against the books as executed


def snapshot_holdings(store, venue, acct, source, coins=None):
    """What the account holds now, for every coin the market data covers (the built-in coins, a strategy file's
    coins, and anything the books still hold): the next comparison's expectation."""
    insts = venue.market.instruments()
    coins = coins or coins_of(insts)
    snap = {}
    for coin in coins:
        si = insts.get((coin, "spot"))
        snap[coin] = {"perp": acct.perp_size(coin), "spot": acct.spot_size(si.token) if si else 0.0}
    store.put("expected_holdings", {"ts": time.time(), "source": source, "coins": snap})
    return snap


def mark_holdings_pending(store, source):
    """Before the bot itself trades: a crash mid-trade must not read as an outside change."""
    exp = store.get("expected_holdings") or {}
    exp["pending"] = source
    store.put("expected_holdings", exp)


def compare_holdings(expected, acct, insts, mids, min_usd, coins=None):
    """[(coin, kind, detail)]: kind 'one_legged' (a carry short shrank, spot intact) or 'unexplained'."""
    out = []
    coins = coins or coins_of(insts)
    for coin in coins:
        e = expected.get(coin, {"perp": 0.0, "spot": 0.0})
        pi, si = insts.get((coin, "perp")), insts.get((coin, "spot"))
        pa = acct.perp_size(coin)
        sa = acct.spot_size(si.token) if si else 0.0
        mid_p = mids[pi.name] if pi else 0.0
        mid_s = mids[si.name] if si else mid_p
        tol_p = max(min_usd / mid_p if mid_p else 0.0, HEDGE_REL_TOL * abs(e["perp"]), 10 ** -(pi.sz_decimals if pi else 0))
        tol_s = max(min_usd / mid_s if mid_s else 0.0, HEDGE_REL_TOL * abs(e["spot"]), 10 ** -(si.sz_decimals if si else 0))
        dp, ds = pa - e["perp"], sa - e["spot"]
        if abs(dp) <= tol_p and abs(ds) <= tol_s:
            continue
        detail = {"expected": e, "actual": {"perp": pa, "spot": sa}, "perp_diff": dp, "spot_diff": ds}
        if e["spot"] > tol_s and abs(ds) <= tol_s and dp > tol_p:
            detail["unhedged"] = min(dp, sa)
            out.append((coin, "one_legged", detail))
        else:
            out.append((coin, "unexplained", detail))
    return out


def check_holdings(cfg, venue, store, notifier, acct, day, halt, is_halted):
    """Compare, then repair a one-legged carry or halt on anything else. Returns a summary dict."""
    exp = store.get("expected_holdings")
    if not exp or not exp.get("coins"):
        snapshot_holdings(store, venue, acct, "first observation")
        return {"status": "baseline"}
    if exp.get("pending"):
        # The last trading process stopped before recording what it left; the lock is ours now, so nobody else
        # trades. Its orders are in the records; take the account as it stands.
        snapshot_holdings(store, venue, acct, f"after interrupted {exp['pending']}")
        return {"status": "rebaselined", "after": exp["pending"]}
    insts = venue.market.instruments()
    mids = venue.market.mids()
    found = compare_holdings(exp["coins"], acct, insts, mids, cfg.limits.min_order_usdc)
    if not found:
        return {"status": "ok"}
    unexplained = [f for f in found if f[1] == "unexplained"]
    if unexplained:
        halt("venue holdings differ from the books: " + ", ".join(c for c, _, _ in unexplained))
        store.add_guard_action("holdings_mismatch", None, None, False, {"found": found})
        notifier.alert(f"holdings-mismatch:{day}", "stopped: Hyperliquid holds something the books do not explain",
                       f"Expected vs actual: {[(c, d['expected'], d['actual']) for c, _, d in unexplained]}. Trading "
                       "is halted; nothing was closed.",
                       "Tell your assistant whether these positions were changed by hand; it resumes or closes "
                       "them on your word.", window_hours=24)
        return {"status": "halted", "found": found}
    repaired = []
    for coin, _kind, d in found:
        result = {"coin": coin, "unhedged": d["unhedged"], "action": None}
        if is_halted():
            result["action"] = "none (trading halted)"
        else:
            result.update(_restore_hedge(cfg, venue, store, coin, d["unhedged"], day))
        repaired.append(result)
        store.add_guard_action("one_legged_carry", coin, d["unhedged"], result["action"] != "failed", result)
    snapshot_holdings(store, venue, venue.account(), "after hedge repair")
    for r in repaired:
        notifier.alert(f"one-legged:{day}:{r['coin']}",
                       f"Hyperliquid closed part of the {r['coin']} carry short",
                       f"The {r['coin']} perp short shrank by {r['unhedged']:.6f} while the spot leg stayed "
                       f"(forced deleveraging or a liquidation). Action: {r['action']}.",
                       "Nothing, unless it says the repair failed: then tell your assistant to close the "
                       f"{r['coin']} position.", window_hours=12)
    return {"status": "repaired", "repaired": repaired}


def _restore_hedge(cfg, venue, store, coin, unhedged, day):
    insts = venue.market.instruments()
    mids = venue.market.mids()
    pi, si = insts[(coin, "perp")], insts[(coin, "spot")]
    ex = Executor(cfg, venue, store, day, purpose="restore_hedge")
    size = pi.round_size(-unhedged)
    if size and abs(size) * mids[pi.name] >= cfg.limits.min_order_usdc:
        leg = Leg(coin, pi, size, mids[pi.name], purpose="restore_hedge")
        ex.run_group([leg], unwind=True)
        ex.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "failed"))
        if leg.fill_ratio >= 0.9:
            return {"action": f"re-shorted {abs(leg.filled):.6f} {coin} perp"}
        unhedged -= abs(leg.filled)
    # The market did not allow it: sell the orphaned spot instead.
    size = si.round_size(-unhedged)
    if not size or abs(size) * mids[si.name] < cfg.limits.min_order_usdc:
        return {"action": "left as is (too small to trade)"}
    leg = Leg(coin, si, size, mids[si.name], purpose="sell_orphaned_spot")
    ex.run_group([leg], unwind=True)
    ex.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "failed"))
    if leg.fill_ratio >= 0.9:
        return {"action": f"could not re-short; sold {abs(leg.filled):.6f} orphaned {si.token}"}
    return {"action": "failed", "detail": "neither re-shorting nor selling the spot filled"}


# ---- 3. Unit bridge peg


def observe_peg(store, venue, now_ts, coins=("BTC", "ETH", "SOL")):
    """Log spot mid against perp mark per coin and update the 15-minute states. Returns {coin: state}."""
    insts = venue.market.instruments()
    mids = venue.market.mids()
    marks = venue.market.perp_marks()
    st = store.get("peg") or {}
    for coin in coins:
        si = insts.get((coin, "spot"))
        if si is None or coin not in marks or si.name not in mids:
            continue
        gap = mids[si.name] / marks[coin] - 1.0
        store.add_peg_gap(coin, mids[si.name], marks[coin], gap, now_ts)
        s = st.setdefault(coin, {"above1_since": None, "below1_since": None, "above3_since": None,
                                 "blocked": False, "exit": False})
        s["gap"], s["at"] = gap, now_ts
        if abs(gap) > PEG_BLOCK:
            s["above1_since"] = s["above1_since"] or now_ts
            s["below1_since"] = None
        else:
            s["above1_since"] = None
            s["below1_since"] = s["below1_since"] or now_ts
        s["above3_since"] = (s["above3_since"] or now_ts) if abs(gap) > PEG_EXIT else None
        if s["above1_since"] and now_ts - s["above1_since"] >= PEG_WINDOW_S:
            s["blocked"] = True
        if s["blocked"] and s["below1_since"] and now_ts - s["below1_since"] >= PEG_WINDOW_S:
            s["blocked"] = False
        s["exit"] = bool(s["above3_since"] and now_ts - s["above3_since"] >= PEG_WINDOW_S)
    store.put("peg", st)
    return st


def exit_depegged_carry(cfg, venue, store, notifier, acct, peg, day):
    """Close the carry pairs of every coin whose spot stayed more than 3% off the perp for 15 minutes."""
    insts = venue.market.instruments()
    mids = venue.market.mids()
    closed = {}
    for coin, s in peg.items():
        if not s.get("exit"):
            continue
        pi, si = insts[(coin, "perp")], insts[(coin, "spot")]
        spot = acct.spot_size(si.token)
        perp = acct.perp_size(coin)
        hedge = min(spot, -perp) if perp < 0 else 0.0
        legs = []
        if spot and abs(spot) * mids[si.name] >= cfg.limits.min_order_usdc:
            legs.append(Leg(coin, si, si.round_size(-spot), mids[si.name], purpose="peg_exit", pair=f"{coin}-peg"))
        if hedge > 0 and hedge * mids[pi.name] >= cfg.limits.min_order_usdc:
            legs.append(Leg(coin, pi, pi.round_size(hedge), mids[pi.name], purpose="peg_exit", pair=f"{coin}-peg"))
        legs = [leg for leg in legs if leg.size]
        if not legs:
            continue
        ex = Executor(cfg, venue, store, day, purpose="peg_exit")
        ex.run_group(legs, unwind=True)
        for leg in legs:
            ex.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "failed"))
        closed[coin] = {leg.inst.key: leg.filled for leg in legs}
        store.add_guard_action("peg_exit", coin, s["gap"], all(leg.fill_ratio >= 0.9 for leg in legs),
                               {"gap": s["gap"], "filled": closed[coin]})
        notifier.alert(f"peg-exit:{day}:{coin}", f"closed its {coin} carry trade: {si.token} lost its peg",
                       f"{si.token} traded {s['gap'] * 100:+.2f}% away from the {coin} perp for more than 15 minutes. "
                       f"The bot sold the {si.token} and bought back the short: {closed[coin]}. No new {coin} carry "
                       "until the gap is back under 1% for 15 minutes.",
                       "Nothing, unless you want the bot to stay out of Unit tokens for good: then tell Mission "
                       "Control.", window_hours=24)
    return closed


# ---- what the checks allow the daily run to open


def restrict_targets(targets, acct, insts, peg=None, no_new_exposure=False, live_gaps=None):
    """Apply the blocks to the account targets {coin: (perp, spot)}.

    no_new_exposure (terms changed): no position may grow or flip; reductions are allowed.
    peg blocked (or a gap above 1% right now): the coin's carry may shrink but not grow.
    peg exit: the coin's carry target is zero.
    The perp carries the carry's short, so it moves by whatever the spot target loses.
    """
    out, notes = {}, []
    peg = peg or {}
    live_gaps = live_gaps or {}
    for coin, (perp_t, spot_t) in targets.items():
        si = insts.get((coin, "spot"))
        spot_a = acct.spot_size(si.token) if si else 0.0
        perp_a = acct.perp_size(coin)
        p, s = perp_t, spot_t
        state = peg.get(coin, {})
        if state.get("exit"):
            p, s = p + s, 0.0
            notes.append(f"{coin}: carry closed (peg gap above 3%)")
        elif (state.get("blocked") or abs(live_gaps.get(coin, 0.0)) > PEG_BLOCK) and s > spot_a:
            p, s = p + (s - spot_a), spot_a
            notes.append(f"{coin}: no new carry (peg gap above 1%)")
        if no_new_exposure:
            if s > spot_a:
                p, s = p + (s - max(spot_a, 0.0)), max(spot_a, 0.0)
            if perp_a == 0 or (p > 0) != (perp_a > 0):
                p2 = 0.0
            else:
                p2 = max(min(abs(p), abs(perp_a)), 0.0) * (1 if perp_a > 0 else -1)
            if p2 != p:
                notes.append(f"{coin}: perp held at {p2:.6f} instead of {p:.6f} (no new positions)")
            p = p2
        out[coin] = (p, s)
    return out, notes
