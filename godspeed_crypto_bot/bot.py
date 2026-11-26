"""The daily run, the five-minute guard, flatten, the deadline check and the drawdown halt.

Daily run (00:02 UTC):
  1. Claim the day's run in SQLite. A finished run is never repeated; an unfinished one resumes.
  2. Read yesterday's closed candles and funding (data.build_history).
  3. Live: check the API wallet's expiry. Book yesterday on the shadow ledger, read the account and compare
     it with the books as last executed (forced deleveraging, one-legged carry, anything unexplained).
  4. Size the strategy at min(account, cap); drawdown check; decide (strategy.decide); record decisions.
  5. Net the books per coin, apply the blocks (peg, changed terms), plan orders (limits, minimum size),
     execute (a carry pair's legs back to back), record; snapshot what the account now holds.
  6. Advance the shadow ledger at the day's open (the backtest's fill) and save the books.
A rerun after a crash reuses the decision saved in step 4, so the same inputs give the same targets,
and every order is target minus actual, so nothing is traded twice.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from . import godspeed, outbox, page, records, safety, strategy_file
from .data import DataNotReady, build_history, day_ms
from .execution import Executor, Leg, plan_orders
from .keys import LiveNotAllowed, live_problems, load_agent_key
from .ledger import Shadow, ShadowState
from .names import DISPLAY_NAME
from .strategy import config_for, decide
from .venue.base import TWO_BALANCE_MODES, coins_of
from .venue.hyperliquid import HyperliquidMarketData, HyperliquidVenue
from .venue.simulated import SimulatedVenue

log = logging.getLogger(__name__)
COINS = ("BTC", "ETH", "SOL")  # the built-in strategies' coins: always read, so a switch can close what they hold
FINAL = ("complete", "halted", "skipped")
TWO_WALLET_MODES = TWO_BALANCE_MODES  # transfers between spot and perp only here; Unified has one USDC balance


def utcnow():
    return datetime.now(timezone.utc)


# ---- construction


def coins_for(cfg, store=None, spec=None):
    """The coins a run reads and the checks watch: the built-in coins, the strategy file's coins, and every coin the
    books or the last holdings snapshot still hold, so that a switch of strategy can close it."""
    out = list(COINS)
    if spec is None and strategy_file.is_file_setting(cfg.strategy):
        try:
            spec = strategy_file.load_for(cfg)
        except strategy_file.StrategyFileError:
            spec = None
    if spec is not None:
        out += spec.coins
    if store is not None:
        _day, state = store.books_before("9999-12-31")
        books = (state or {}).get("books") or {}
        for book, key in (("trend", "q"), ("carry", "perp_q"), ("carry", "spot_q")):
            out += [c for c, q in ((books.get(book) or {}).get(key) or {}).items() if q]
        held = (store.get("expected_holdings") or {}).get("coins") or {}
        out += [c for c, legs in held.items() if isinstance(legs, dict) and any(legs.get(k) for k in ("perp", "spot"))]
    return tuple(dict.fromkeys(c for c in out if isinstance(c, str)))


def build_venue(cfg, store, market=None, before_confirmation=False):
    market = market or HyperliquidMarketData(cfg.network, cfg.spot_tokens, coins_for(cfg, store))
    if cfg.mode == "dry-run":
        return SimulatedVenue(market, store, "model", start_usdc=cfg.capital_cap_usdc, leverage=cfg.limits.leverage)
    if cfg.mode == "paper":
        return SimulatedVenue(market, store, "book", start_usdc=cfg.capital_cap_usdc, leverage=cfg.limits.leverage)
    problems = live_problems(cfg, before_confirmation=before_confirmation)  # True only for `preflight`, which never trades
    if problems:
        raise LiveNotAllowed(problems)
    return HyperliquidVenue(market, cfg.master_address, load_agent_key(cfg),
                            allow_missing_spot=cfg.network == "testnet" and cfg.allow_missing_spot)


def open_store(cfg):
    return records.Store(cfg.db_path, cfg.mode, cfg.network)


@contextmanager
def trading_lock(cfg, wait=True, timeout=600):
    """One process trades at a time (daily run, guard, flatten). Yields False if not acquired."""
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    fh = open(cfg.state_path / "trading.lock", "a+")
    try:
        try:
            import fcntl
        except ImportError:  # Windows (tests): one process only
            yield True
            return
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait or time.time() > deadline:
                    yield False
                    return
                time.sleep(1)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


# ---- halt


def halted(cfg):
    return cfg.halt_path.exists()


def write_halt(cfg, reason):
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    if not cfg.halt_path.exists():
        cfg.halt_path.write_text(f"{utcnow().isoformat(timespec='seconds')} {reason}\n", encoding="utf-8")


def _agent_halt(cfg, store, notifier, error, day):
    write_halt(cfg, f"Hyperliquid refused the API wallet: {error}")
    notifier.alert(f"agent-refused:{day}", "stopped: Hyperliquid refused its API wallet",
                   f"Signing failed with: {error}. The wallet is expired or no longer approved. Trading is "
                   "halted; positions are untouched.",
                   "Approve a new API wallet for the bot on Hyperliquid (your assistant makes its address with keygen).",
                   window_hours=24)


# ---- account helpers


def spot_prices(venue):
    """Strategy spot token -> mid (only UBTC, UETH, USOL count toward the strategy's money)."""
    mids = venue.market.mids()
    return {inst.token: mids[inst.name] for (_c, m), inst in venue.market.instruments().items()
            if m == "spot" and inst.name in mids}


def account_value(venue, acct):
    return acct.account_value(spot_prices(venue))


def drawdown_state(cfg, store, venue, acct, value):
    start = store.get("start")
    if not start:
        return None
    try:
        flows = venue.external_flows(int(start["ts_ms"]))
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger must not hide a drawdown
        log.warning("could not read deposits and withdrawals: %s", exc)
        flows = 0.0
    pnl = value - start["account_value"] - flows
    equity = start["capital"] + pnl
    floor = (1 - cfg.limits.drawdown_halt) * start["capital"]
    return {"equity": equity, "floor": floor, "breached": equity < floor, "flows": flows, "pnl": pnl,
            "capital": start["capital"]}


def sync_records(cfg, venue, store, now):
    """Funding on the simulated account; the venue's own fills, funding and ledger (with hashes) when live."""
    if isinstance(venue, SimulatedVenue):
        venue.sync_funding(int(now.timestamp() * 1000))
        return {}
    end = int(now.timestamp() * 1000)
    last = store.get("records_synced_ms") or int((now - timedelta(days=3)).timestamp() * 1000)
    out = venue.sync_records(store, last - 3 * 3_600_000, end)
    store.put("records_synced_ms", end)
    return out


# ---- flatten


def flatten(cfg, venue, store, notifier, reason, day=None):
    """Close every strategy position (perps reduce-only, Unit spot tokens sold), then halt. Returns residuals."""
    write_halt(cfg, f"flatten: {reason}")
    day = day or utcnow().date()
    safety.mark_holdings_pending(store, "flatten")
    acct = venue.account()
    insts = venue.market.instruments()
    mids = venue.market.mids()
    groups = {}
    for coin in coins_of(insts):
        pi, si = insts.get((coin, "perp")), insts.get((coin, "spot"))
        group = []
        if pi and acct.perp_size(coin):
            size = pi.round_size(-acct.perp_size(coin))
            if size:
                group.append(Leg(coin, pi, size, mids[pi.name], purpose="flatten", reduce_only=True))
        if si and acct.spot_size(si.token):
            size = si.round_size(-acct.spot_size(si.token))
            if size and abs(size) * mids[si.name] >= cfg.limits.min_order_usdc:
                group.append(Leg(coin, si, size, mids[si.name], purpose="flatten"))
        for leg in group:
            leg.pair = f"{coin}-flatten" if len(group) == 2 else None
            groups.setdefault(leg.pair or f"{coin}-{leg.market}", []).append(leg)
    ex = Executor(cfg, venue, store, day, purpose="flatten")
    for g in groups.values():
        ex.run_group(g, unwind=True)
        for leg in g:
            ex.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "failed"))
    if acct.mode in TWO_WALLET_MODES:
        try:
            ex.sweep_spot_usdc(cfg.guard.spot_usdc_reserve)
        except Exception as exc:  # noqa: BLE001
            log.warning("sweep after flatten failed: %s", exc)
    after = venue.account()
    safety.snapshot_holdings(store, venue, after, "after flatten")
    mids = venue.market.mids()
    residual = {}
    for coin in coins_of(insts):
        pi, si = insts.get((coin, "perp")), insts.get((coin, "spot"))
        if pi and abs(after.perp_size(coin)) * mids[pi.name] >= cfg.limits.min_order_usdc:
            residual[f"perp:{coin}"] = after.perp_size(coin)
        if si and abs(after.spot_size(si.token)) * mids[si.name] >= cfg.limits.min_order_usdc:
            residual[f"spot:{coin}"] = after.spot_size(si.token)
    store.add_guard_action("flatten", None, None, not residual, {"reason": reason, "residual": residual})
    if residual:
        notifier.alert(f"flatten-incomplete:{day}", "flatten could not close every position",
                       f"Still open after {cfg.limits.retries} attempts each: {residual}. Trading is halted.",
                       "Tell your assistant to close the remaining crypto positions by hand on Hyperliquid.",
                       window_hours=6)
    return residual


def halt_and_flatten(cfg, venue, store, notifier, dd, day):
    log.error("drawdown halt: equity %.2f below floor %.2f", dd["equity"], dd["floor"])
    residual = flatten(cfg, venue, store, notifier, f"drawdown: equity {dd['equity']:.2f} < {dd['floor']:.2f}", day)
    notifier.alert(
        f"drawdown:{day}",
        f"stopped after a {cfg.limits.drawdown_halt:.0%} loss and closed its positions",
        f"Strategy equity {dd['equity']:.2f} USDC is below {dd['floor']:.2f} (start {dd['capital']:.2f}). "
        f"Everything was closed{'' if not residual else ', except ' + str(residual)}; trading is halted.",
        f"Decide whether {DISPLAY_NAME} should ever trade again; it stays off until you say so.",
        window_hours=24 * 30,
    )


# ---- the daily run


def _settle_missed(shadow, snap, through: date):
    """Book every day from the last settled one through ``through`` (positions unchanged on missed days)."""
    last = date.fromisoformat(shadow.s.last_settled) if shadow.s.last_settled else through - timedelta(days=1)
    d = last + timedelta(days=1)
    while d <= through:
        close = {c: snap.close_perp[c][d] for c in snap.close_perp if d in snap.close_perp[c]}
        for c, days in snap.close_perp.items():  # a day without a candle (a quiet coin): marked at its last close
            if c not in close and any(k < d for k in days):
                close[c] = days[max(k for k in days if k < d)]
        for key, px in shadow.s.marks.items():  # a held coin the exchange no longer lists stays at its last mark
            market, coin = key.split("@")[0].split(":")
            if market == "perp":
                close.setdefault(coin, px)
        spot_close = {c: snap.close_spot[c][d] for c in snap.close_spot if d in snap.close_spot[c]}
        for key, px in shadow.s.marks.items():
            market, coin = key.split("@")[0].split(":")
            if market == "spot":
                spot_close.setdefault(coin, px)
        funding = {c: snap.funding.get(c, {}).get(d, 0.0) for c in snap.close_perp}
        shadow.settle(d, close, spot_close, funding)
        d += timedelta(days=1)


def run_daily(cfg, venue, store, notifier, now=None, allow_late=False, market_snapshot=None):
    now = now or utcnow()
    day = now.date()
    row = store.run(day)
    if row is not None and row["status"] in FINAL:
        return {"day": str(day), "status": row["status"], "note": "already done today; nothing traded again"}
    if not allow_late and now.hour >= cfg.latest_run_hour_utc:
        row, _ = store.start_run(day)
        store.update_run(row["id"], status="skipped", finished_at=time.time(),
                         error=f"started at {now:%H:%M} UTC, after the {cfg.latest_run_hour_utc}:00 cut-off")
        return {"day": str(day), "status": "skipped", "note": "too late to trade today's decision"}
    with trading_lock(cfg) as got:
        if not got:
            raise RuntimeError("another trading process holds the lock")
        row, _fresh = store.start_run(day)
        if row["status"] in FINAL:
            return {"day": str(day), "status": row["status"], "note": "already done today; nothing traded again"}
        run_id = row["id"]
        if halted(cfg):
            store.update_run(run_id, status="halted", finished_at=time.time(),
                             error=cfg.halt_path.read_text(encoding="utf-8").strip())
            return {"day": str(day), "status": "halted", "note": "HALT file present; no trading"}
        try:
            return _daily(cfg, venue, store, notifier, now, day, run_id, row, market_snapshot)
        except DataNotReady as exc:
            store.update_run(run_id, status="waiting_for_data", error=str(exc))
            raise
        except Exception as exc:
            store.update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise


def _halt_run(store, run_id, reason, summary=None):
    store.update_run(run_id, status="halted", finished_at=time.time(), error=reason, summary=summary or {})
    return {"status": "halted", "note": reason, **(summary or {})}


def _daily(cfg, venue, store, notifier, now, day, run_id, row, market_snapshot):
    spec = strategy_file.load_for(cfg)  # None for a built-in; a file the bot refuses stops the run with its sentence
    if market_snapshot is None:
        days = max(cfg.history_days, spec.history_days()) if spec else cfg.history_days
        market_snapshot = build_history(venue.market, day, coins_for(cfg, store, spec), days,
                                        funding_days=spec.funding_days() if spec else 12, required=COINS)
    history, snap = market_snapshot
    insts = venue.market.instruments()
    sync_records(cfg, venue, store, now)
    checks = {}

    def halt(reason):
        write_halt(cfg, reason)

    if venue.signs:
        checks["agent"] = safety.check_agent_expiry(cfg, store, notifier, venue, now, halt)
        if halted(cfg):
            return _halt_run(store, run_id, "API wallet expired or not approved", {"checks": checks})

    if row["pre_state"]:  # a resumed run: the same inputs as the first attempt
        pre = json.loads(row["pre_state"])
        shadow = Shadow(ShadowState.from_dict(pre["shadow"]))
        equity = pre["equity"]
        acct = venue.account()
        checks["holdings"] = safety.check_holdings(cfg, venue, store, notifier, acct, day, halt,
                                                   lambda: halted(cfg))
    else:
        _prev_day, prev = store.books_before(day)
        shadow = None
        if prev is not None:
            shadow = Shadow(ShadowState.from_dict(prev))
            _settle_missed(shadow, snap, day - timedelta(days=1))
        acct = venue.account()
        checks["holdings"] = safety.check_holdings(cfg, venue, store, notifier, acct, day, halt,
                                                   lambda: halted(cfg))
        if halted(cfg):
            return _halt_run(store, run_id, "venue holdings differ from the books", {"checks": checks})
        acct = venue.account()
        value = account_value(venue, acct)
        start = store.get("start")
        if start is None:
            start = {"day": str(day), "ts_ms": int(now.timestamp() * 1000), "account_value": value,
                     "capital": min(value, cfg.capital_cap_usdc)}
            store.put("start", start)
        if shadow is None:
            shadow = Shadow(ShadowState(strategy_file.fresh_books(start["capital"], day, cfg.strategy, spec),
                                        last_settled=str(day - timedelta(days=1))))
        equity = min(value, cfg.capital_cap_usdc)
        dd = drawdown_state(cfg, store, venue, acct, value)
        yday = day - timedelta(days=1)
        peg_log = store.peg_summary(datetime(yday.year, yday.month, yday.day, tzinfo=timezone.utc).timestamp(),
                                    datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        store.put_equity(yday, "account", value, {"strategy_equity": equity, "drawdown": dd, "peg_gaps": peg_log,
                                                  "at": now.isoformat(timespec="seconds")})
        _record_shadow_equity(store, shadow, yday)
        if dd and dd["breached"]:
            halt_and_flatten(cfg, venue, store, notifier, dd, day)
            return _halt_run(store, run_id, "drawdown halt; flattened")
        store.update_run(run_id, pre_state={"shadow": shadow.s.to_dict(), "equity": equity, "account_value": value})

    if spec is None:
        decision = decide(history, shadow.s.books, equity=equity, config=config_for(cfg.strategy))
        trades_today = True
    else:
        # A weekly or monthly file strategy decides on its days (and on any day its book has grown past its limits);
        # between them the account only follows its targets down (a lower cap, less money), never up.
        trades_today = strategy_file.trades_on(history, shadow.s.books, spec, day)
        decision = strategy_file.decide(history, shadow.s.books, spec, equity=equity, day=day)
    decision_doc = decision.to_dict()
    decision_doc["warnings"] = snap.warnings
    store.update_run(run_id, status="decided", decision=decision_doc)
    _record_decisions(store, run_id, day, shadow.s.books, decision)

    # What the account may do today: peg and terms blocks.
    peg = safety.observe_peg(store, venue, now.timestamp())
    gaps = {c: s.get("gap", 0.0) for c, s in peg.items()}
    block = store.get("entry_block") or {}
    acct = venue.account()
    targets, notes = safety.restrict_targets(decision.account_targets(), acct, insts, peg, bool(block), gaps)
    mids = venue.market.mids()
    if not trades_today:
        targets = _reductions_only(targets, acct, insts)
        notes = [*notes, f"not a {spec.rebalance} trading day for {spec.name}: positions only reduced where its "
                         "targets shrank"]
    plan = plan_orders(targets, acct, insts, mids, cfg, coins_of(insts))
    ex = Executor(cfg, venue, store, day, run_id)
    for coin, market, size, mid, reason in plan.skipped:
        store.add_order(run_id, day, "rebalance", coin, market, "buy" if size > 0 else "sell", abs(size), mid,
                        "skipped_min_size", reason)
    for coin, market, size, clipped, _mid in plan.clipped:
        log.warning("%s %s order %.6f clipped to %.6f by the per-order cap", coin, market, size, clipped)
    failures = []
    if plan.legs:
        store.update_run(run_id, status="executing")
        safety.mark_holdings_pending(store, f"daily run {day}")
        for coin in sorted({leg.coin for leg in plan.legs if leg.market == "perp"}):
            try:
                venue.set_leverage(coin, cfg.limits.leverage)
            except Exception as exc:  # noqa: BLE001 - an order would then fail on margin and be recorded
                log.warning("leverage for %s: %s", coin, exc)
                if safety.is_agent_failure(str(exc)):
                    ex.agent_failure = str(exc)
        if not ex.agent_failure:
            if acct.mode in TWO_WALLET_MODES:
                ex.balance_before_trading(plan, acct, cfg.guard.spot_usdc_reserve)
            failures = ex.execute_plan(plan)
            if acct.mode in TWO_WALLET_MODES and not ex.agent_failure:
                ex.sweep_spot_usdc(cfg.guard.spot_usdc_reserve)
    after = venue.account()
    safety.snapshot_holdings(store, venue, after, f"after daily run {day}")
    if ex.agent_failure:
        _agent_halt(cfg, store, notifier, ex.agent_failure, day)

    balance = shadow.execute(decision, snap.open_perp or _mid_by_coin(venue, "perp"),
                             snap.open_spot or _mid_by_coin(venue, "spot"))
    store.save_books(day, shadow.s.to_dict())
    store.put_equity(day, "account_after_trades", account_value(venue, after),
                     {"positions": {c: p.size for c, p in after.positions.items()},
                      "spot": {t: v[0] for t, v in after.spot.items() if t in spot_prices(venue) or t == "USDC"}})
    sync_records(cfg, venue, store, utcnow() if not isinstance(venue, SimulatedVenue) else now)

    for f in failures:
        if f["unwound"]:
            notifier.alert(f"leg-failure:{day}:{f['coin']}", f"a {f['coin']} carry trade half-filled and was undone",
                           f"One leg of the {f['coin']} spot/perp pair did not fill: {f['legs']}. The filled leg was "
                           "reversed; the bot tries again tomorrow.",
                           "Nothing today. If this repeats tomorrow, ask your assistant to look at the carry trades.")
        else:
            notifier.alert(f"unhedged:{day}:{f['coin']}", f"holds an unhedged {f['coin']} position",
                           f"A {f['coin']} carry leg failed and reversing the other leg also failed. Left open: "
                           f"{f['residual']}.",
                           "Tell your assistant to close the open crypto position now (the bot's flatten command).")
    summary = {
        "day": str(day), "mode": cfg.mode, "network": cfg.network,
        "status": "halted" if ex.agent_failure else "complete",
        "strategy_equity": equity, "scale": decision.scale, "reset": decision.reset,
        "trend_orders": decision.trend_orders, "carry_orders": decision.carry_orders,
        "carry_active": decision.carry_active, "carry_funding_7d": decision.carry_funding_7d,
        "targets": {c: {"perp": p, "spot": s} for c, (p, s) in targets.items()},
        "restrictions": notes, "entry_block": block, "peg_gaps": gaps, "checks": checks,
        "orders": [dict(r) for r in store.orders_for_run(run_id)],
        "pair_failures": failures, "warnings": snap.warnings, "shadow_balance_prices": balance,
    }
    store.update_run(run_id, status=summary["status"], finished_at=time.time(), summary=summary,
                     error=ex.agent_failure)
    return summary


def _reductions_only(targets, acct, insts):
    """Targets that only ever make a position smaller: a perpetual moves toward its target only when that is a
    reduction on the same side (or closing it); everything else, spot included, stays as the account holds it."""
    out = {}
    for coin in coins_of(insts):
        perp_t, _spot_t = targets.get(coin, (0.0, 0.0))
        si = insts.get((coin, "spot"))
        held = acct.perp_size(coin)
        shrinks = held and (perp_t == 0 or (perp_t > 0) == (held > 0)) and abs(perp_t) < abs(held)
        out[coin] = (perp_t if shrinks else held, acct.spot_size(si.token) if si else 0.0)
    return out


def _mid_by_coin(venue, market):
    mids = venue.market.mids()
    return {c: mids[i.name] for (c, m), i in venue.market.instruments().items() if m == market}


def _record_shadow_equity(store, shadow, day):
    b = shadow.s.books
    buckets = {k: v.to_dict() for k, v in shadow.s.buckets.items()}
    store.put_equity(day, "shadow_trend", b.trend.equity, buckets["trend"])
    store.put_equity(day, "shadow_carry", b.carry.equity, buckets["carry"])
    store.put_equity(day, "shadow_total", b.equity, {})


def _record_decisions(store, run_id, day, books, decision):
    for c in sorted(set(books.trend.q) | set(decision.trend_orders) | set(decision.trend_weights)):
        store.add_decision(run_id, day, "trend", c, books.trend.q.get(c, 0.0),
                           decision.books_after.trend.q.get(c, 0.0), c in decision.trend_orders,
                           {"weight": decision.trend_weights.get(c)})
    for c in sorted(decision.carry_funding_7d):
        store.add_decision(run_id, day, "carry", c, books.carry.spot_q.get(c, 0.0),
                           decision.books_after.carry.spot_q.get(c, 0.0), c in decision.carry_orders,
                           {"funding_7d_annual": decision.carry_funding_7d[c],
                            "eligible": decision.carry_eligible.get(c), "active": c in decision.carry_active})


# ---- deadline check (00:45 UTC), which is also the daily API-wallet expiry check


def check_deadline(cfg, store, notifier, now=None, venue=None):
    now = now or utcnow()
    day = now.date()
    out = {"day": str(day)}
    if venue is not None and venue.signs:
        out["agent"] = safety.check_agent_expiry(cfg, store, notifier, venue, now, lambda r: write_halt(cfg, r))
    row = store.run(day)
    if row is not None and row["status"] in ("complete", "halted"):
        out.update(ok=True, status=row["status"])
        return out
    status = row["status"] if row else "never started"
    error = row["error"] if row else None
    notifier.alert(f"deadline:{day}", f"did not finish today's trading run by {cfg.deadline_utc} UTC",
                   f"Run status: {status}. Error: {error or 'none recorded'}. No trade was made for today's decision.",
                   f"Ask your assistant to check {DISPLAY_NAME}'s daily run; positions stay as they were.")
    out.update(ok=False, status=status, error=error)
    return out


# ---- the guard (every five minutes)


def guard(cfg, venue, store, notifier, now=None):
    now = now or utcnow()
    with trading_lock(cfg, wait=False) as got:
        if not got:
            return {"status": "skipped", "note": "the daily run is trading"}
        sync_records(cfg, venue, store, now)
        acct = venue.account()
        out = {}

        def halt(reason):
            write_halt(cfg, reason)

        out["holdings"] = safety.check_holdings(cfg, venue, store, notifier, acct, now.date(), halt,
                                                lambda: halted(cfg))
        acct = venue.account()
        peg = safety.observe_peg(store, venue, now.timestamp())
        out["peg"] = {c: {"gap": round(s.get("gap", 0.0), 5), "blocked": s["blocked"], "exit": s["exit"]}
                      for c, s in peg.items()}
        if any(s.get("exit") for s in peg.values()):
            if halted(cfg):
                notifier.alert(f"peg-exit-halted:{now.date()}", "a Unit token lost its peg while trading is halted",
                               f"Gaps: {out['peg']}. Trading is halted, so the carry pairs were not closed.",
                               "Tell your assistant whether to close the carry trades now.", window_hours=12)
            else:
                safety.mark_holdings_pending(store, "peg exit")
                out["peg_exit"] = safety.exit_depegged_carry(cfg, venue, store, notifier, acct, peg, now.date())
                acct = venue.account()
                safety.snapshot_holdings(store, venue, acct, "after peg exit")
        value = account_value(venue, acct)
        dd = drawdown_state(cfg, store, venue, acct, value)
        if dd and dd["breached"] and not halted(cfg):
            halt_and_flatten(cfg, venue, store, notifier, dd, now.date())
            return {**out, "status": "halted", "drawdown": dd}
        out.update(_margin(cfg, venue, store, notifier, acct, now))
        return out


def _margin(cfg, venue, store, notifier, acct, now):
    g = cfg.guard
    lev = acct.perp_notional / acct.perp_value if acct.perp_value > 0 else (float("inf") if acct.perp_notional else 0)
    maint_bad = acct.maint_margin_used > 0 and acct.perp_value < g.maint_multiple * acct.maint_margin_used
    if lev <= g.trigger_leverage and not maint_bad:
        return {"status": "ok", "leverage": lev}
    before = {"leverage": lev, "perp_value": acct.perp_value, "maint": acct.maint_margin_used,
              "notional": acct.perp_notional}
    # 1. Free spot USDC to the perp balance (moving money within the account is not trading).
    need = acct.perp_notional / g.target_leverage - acct.perp_value
    free = acct.spot_usdc_free - g.spot_usdc_reserve
    if need > 0 and free >= 1.0 and acct.mode in TWO_WALLET_MODES:
        amount = round(min(need, free), 2)
        ok = venue.transfer_usdc(amount, to_perp=True)
        store.add_guard_action("transfer_to_perp", None, amount, ok, {"before": before})
        acct = venue.account()
    if _guard_ok(acct, g):
        return {"status": "fixed", "by": "transfer", "before": before}
    # 2. Shrink the carry pairs (sell spot, buy back the same amount of the short), then move the cash over.
    if not halted(cfg):
        safety.mark_holdings_pending(store, "guard shrink")
        shrunk = _shrink_pairs(cfg, venue, store, acct, now.date())
        acct = venue.account()
        safety.snapshot_holdings(store, venue, acct, "after guard shrink")
        if shrunk and _guard_ok(acct, g):
            return {"status": "fixed", "by": "shrink", "before": before, "shrunk": shrunk}
    after = {"leverage": acct.perp_notional / acct.perp_value if acct.perp_value > 0 else None,
             "perp_value": acct.perp_value, "maint": acct.maint_margin_used}
    store.add_guard_action("unfixed", None, None, False, {"before": before, "after": after})
    notifier.alert(f"margin:{now.date()}", "cannot bring its margin back to a safe level",
                   f"Perp leverage {before['leverage']:.2f}x (limit {g.trigger_leverage}x), perp equity "
                   f"{acct.perp_value:.2f} USDC, maintenance margin {acct.maint_margin_used:.2f}. Moving USDC "
                   "and shrinking the carry trades did not fix it.",
                   "Tell your assistant whether to close the crypto positions now (flatten) or add USDC.",
                   window_hours=6)
    return {"status": "unfixed", "before": before, "after": after}


def _guard_ok(acct, g):
    lev = acct.perp_notional / acct.perp_value if acct.perp_value > 0 else float("inf")
    maint_bad = acct.maint_margin_used > 0 and acct.perp_value < g.maint_multiple * acct.maint_margin_used
    return lev <= g.target_leverage + 1e-9 and not maint_bad


def _shrink_pairs(cfg, venue, store, acct, day):
    """Smallest fraction (in steps) of every carry pair to unwind so perp leverage reaches the target."""
    g = cfg.guard
    insts = venue.market.instruments()
    mids = venue.market.mids()
    pairs = []
    for coin in coins_of(insts):
        pi, si = insts.get((coin, "perp")), insts.get((coin, "spot"))
        if not (pi and si):
            continue
        s, p = acct.spot_size(si.token), acct.perp_size(coin)
        if s > 0 and p < 0:
            pairs.append((coin, pi, si, min(s, -p)))
    if not pairs:
        return None
    chosen = None
    for k in range(1, int(round(1 / g.shrink_step)) + 1):
        f = k * g.shrink_step
        notional, value = 0.0, acct.perp_value
        for coin in coins_of(insts):
            pi = insts.get((coin, "perp"))
            if pi is None:
                continue
            q = acct.perp_size(coin)
            for c, _pi, si, hedge in pairs:
                if c == coin:
                    q += f * hedge
                    value += f * hedge * mids[si.name] * (1 - 0.002)
            notional += abs(q) * mids[pi.name]
        chosen = f
        if value > 0 and notional / value <= g.target_leverage:
            break
    ex = Executor(cfg, venue, store, day, purpose="guard")
    done = {}
    for coin, pi, si, hedge in pairs:
        legs = [Leg(coin, pi, pi.round_size(chosen * hedge), mids[pi.name], purpose="guard", pair=f"{coin}-guard"),
                Leg(coin, si, si.round_size(-chosen * hedge), mids[si.name], purpose="guard", pair=f"{coin}-guard")]
        legs = [leg for leg in legs if leg.size and abs(leg.size) * leg.mid >= cfg.limits.min_order_usdc]
        if not legs:
            continue
        ex.run_group(legs, unwind=True)
        for leg in legs:
            ex.finish(leg, "filled" if leg.fill_ratio >= 0.999 else ("partial" if leg.filled else "failed"))
        done[coin] = {leg.inst.key: leg.filled for leg in legs}
        store.add_guard_action("shrink_pair", coin, chosen, all(leg.fill_ratio >= 0.9 for leg in legs),
                               {"fraction": chosen, "filled": done[coin]})
    if acct.mode in TWO_WALLET_MODES:
        ex.sweep_spot_usdc(g.spot_usdc_reserve)
    return done


def status(cfg, store):
    """What the bot is doing, from its records only (no network)."""
    runs = [dict(r) for r in store.last_runs(limit=7)]
    for r in runs:
        for k in ("pre_state", "decision", "summary"):
            r.pop(k, None)
    latest = store.last_runs(limit=1)
    summary = json.loads(latest[0]["summary"]) if latest and latest[0]["summary"] else None
    return {
        "mode": cfg.mode, "network": cfg.network, "strategy": cfg.strategy,
        "strategy_in_words": strategy_file.words(cfg), "halted": halted(cfg),
        "halt_reason": cfg.halt_path.read_text(encoding="utf-8").strip() if halted(cfg) else None,
        "capital_cap_usdc": cfg.capital_cap_usdc, "start": store.get("start"),
        "entry_block": store.get("entry_block") or {}, "agent": store.get("agent"),
        "terms": {k: v for k, v in (store.get("terms") or {}).items() if k != "changed"},
        "peg": store.get("peg"), "expected_holdings": store.get("expected_holdings"),
        "page": page.url(cfg), "godspeed": godspeed.read_status(cfg),
        "messages_waiting": len(outbox.waiting_alerts(cfg)),
        "recent_runs": runs, "latest_summary": summary,
        "shadow_equity": [(r["day"], r["equity"]) for r in store.equity_series("shadow_total")[-10:]],
        "account_equity": [(r["day"], r["equity"]) for r in store.equity_series("account")[-10:]],
        "skipped_orders": store.db.execute("SELECT COUNT(*) FROM skipped_orders WHERE mode=? AND network=?",
                                           (cfg.mode, cfg.network)).fetchone()[0],
        "guard_actions": store.db.execute("SELECT COUNT(*) FROM guard_actions WHERE mode=? AND network=?",
                                          (cfg.mode, cfg.network)).fetchone()[0],
    }


__all__ = ["build_venue", "check_deadline", "coins_for", "day_ms", "flatten", "guard", "open_store", "run_daily",
           "status"]
