"""SQLite records: every decision, intended order, attempt, fill, funding payment, equity, skip and guard action.

One file (bot.sqlite in the state directory). Rows are only added, except a run's status and the
simulated account's single state row. Every row carries the mode and network it belongs to, so a dry-run,
a paper run and a live run never mix.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    attempts INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    pre_state TEXT,
    decision TEXT,
    summary TEXT,
    UNIQUE(day, kind, mode, network)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    day TEXT NOT NULL,
    book TEXT NOT NULL,
    coin TEXT NOT NULL,
    current_q REAL,
    target_q REAL,
    traded INTEGER NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    day TEXT NOT NULL,
    purpose TEXT NOT NULL,
    coin TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    decision_mid REAL,
    notional REAL,
    pair TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    filled REAL NOT NULL DEFAULT 0,
    avg_px REAL,
    fee REAL,
    slippage_bps REAL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    ts REAL NOT NULL,
    attempt INTEGER NOT NULL,
    cloid TEXT,
    size REAL NOT NULL,
    limit_px REAL,
    mid REAL,
    status TEXT NOT NULL,
    filled REAL NOT NULL DEFAULT 0,
    avg_px REAL,
    fee REAL,
    oid INTEGER,
    error TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS funding (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    time_ms INTEGER NOT NULL,
    coin TEXT NOT NULL,
    usdc REAL NOT NULL,
    szi REAL,
    rate REAL,
    source TEXT NOT NULL,
    hash TEXT,
    UNIQUE(mode, network, time_ms, coin)
);
CREATE TABLE IF NOT EXISTS venue_fills (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    time_ms INTEGER NOT NULL,
    coin TEXT NOT NULL,
    market TEXT NOT NULL,
    venue_coin TEXT,
    side TEXT NOT NULL,
    px REAL NOT NULL,
    sz REAL NOT NULL,
    fee REAL,
    fee_token TEXT,
    closed_pnl REAL,
    dir TEXT,
    hash TEXT,
    oid TEXT,
    tid TEXT NOT NULL,
    cloid TEXT,
    raw TEXT,
    UNIQUE(mode, network, tid)
);
CREATE TABLE IF NOT EXISTS venue_ledger (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    time_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    usdc REAL,
    token TEXT,
    amount REAL,
    direction TEXT,
    hash TEXT,
    ref TEXT NOT NULL,
    raw TEXT,
    UNIQUE(mode, network, ref)
);
CREATE TABLE IF NOT EXISTS peg_gaps (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    coin TEXT NOT NULL,
    spot_mid REAL NOT NULL,
    perp_mark REAL NOT NULL,
    gap REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    day TEXT NOT NULL,
    book TEXT NOT NULL,
    equity REAL NOT NULL,
    detail TEXT,
    UNIQUE(mode, network, day, book)
);
CREATE TABLE IF NOT EXISTS guard_actions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    action TEXT NOT NULL,
    coin TEXT,
    amount REAL,
    ok INTEGER NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    key TEXT NOT NULL,
    lane TEXT NOT NULL,
    summary TEXT NOT NULL,
    act TEXT,
    exit_code INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS state (
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (mode, network, name)
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    day TEXT NOT NULL,
    state TEXT NOT NULL,
    UNIQUE(mode, network, day)
);
CREATE TABLE IF NOT EXISTS selftest_steps (
    id INTEGER PRIMARY KEY,
    session TEXT NOT NULL,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    step INTEGER NOT NULL,
    name TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS invocations (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    network TEXT NOT NULL,
    command TEXT NOT NULL,
    unit TEXT,
    every_s INTEGER,
    scheduled_at REAL,
    proc_start REAL,
    main_start REAL NOT NULL,
    finished_at REAL,
    exit_code INTEGER,
    outcome TEXT,
    error TEXT,
    retries TEXT,
    pid INTEGER
);
CREATE TABLE IF NOT EXISTS chat_requests (
    id INTEGER PRIMARY KEY,
    request TEXT NOT NULL,
    at REAL NOT NULL,
    mode TEXT,
    network TEXT,
    kind TEXT,
    strategy TEXT,
    fingerprint TEXT,
    outcome TEXT NOT NULL,
    said TEXT,
    UNIQUE(request, at)
);
CREATE VIEW IF NOT EXISTS skipped_orders AS
    SELECT id, run_id, ts, mode, network, day, purpose, coin, market, side, size, decision_mid, notional, reason
    FROM orders WHERE status LIKE 'skipped%';
"""


class Store:
    def __init__(self, path, mode, network):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode, self.network = mode, network
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(alerts)")}
        for col in ("mode", "network"):  # alerts recorded before 2026-10-17 carry neither
            if col not in cols:
                self.db.execute(f"ALTER TABLE alerts ADD COLUMN {col} TEXT")

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # ---- key/value state per mode and network

    def get(self, name, default=None):
        row = self.db.execute("SELECT value FROM state WHERE mode=? AND network=? AND name=?",
                              (self.mode, self.network, name)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, name, value):
        self.db.execute(
            "INSERT INTO state(mode, network, name, value, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(mode, network, name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (self.mode, self.network, name, json.dumps(value), time.time()),
        )

    # ---- runs

    def run(self, day, kind="daily"):
        return self.db.execute("SELECT * FROM runs WHERE day=? AND kind=? AND mode=? AND network=?",
                               (str(day), kind, self.mode, self.network)).fetchone()

    def start_run(self, day, kind="daily"):
        """Claim the day's run. Returns (row, fresh). A finished run is returned as it is."""
        with self.tx():
            row = self.run(day, kind)
            if row is None:
                self.db.execute(
                    "INSERT INTO runs(day, kind, mode, network, status, started_at) VALUES (?,?,?,?,?,?)",
                    (str(day), kind, self.mode, self.network, "started", time.time()),
                )
                return self.run(day, kind), True
            if row["status"] not in ("complete", "halted", "skipped"):
                self.db.execute("UPDATE runs SET attempts = attempts + 1, error = NULL WHERE id=?", (row["id"],))
            return self.run(day, kind), False

    def update_run(self, run_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*vals, run_id))

    def last_runs(self, kind="daily", limit=10):
        return self.db.execute("SELECT * FROM runs WHERE kind=? AND mode=? AND network=? ORDER BY day DESC LIMIT ?",
                               (kind, self.mode, self.network, limit)).fetchall()

    # ---- books (the virtual books after each day's decision)

    def save_books(self, day, state):
        self.db.execute(
            "INSERT INTO books(mode, network, day, state) VALUES (?,?,?,?) "
            "ON CONFLICT(mode, network, day) DO UPDATE SET state=excluded.state",
            (self.mode, self.network, str(day), json.dumps(state)),
        )

    def books_before(self, day):
        row = self.db.execute(
            "SELECT day, state FROM books WHERE mode=? AND network=? AND day<? ORDER BY day DESC LIMIT 1",
            (self.mode, self.network, str(day)),
        ).fetchone()
        return (row["day"], json.loads(row["state"])) if row else (None, None)

    def books_on(self, day):
        row = self.db.execute("SELECT state FROM books WHERE mode=? AND network=? AND day=?",
                              (self.mode, self.network, str(day))).fetchone()
        return json.loads(row["state"]) if row else None

    # ---- decisions, orders, attempts

    def add_decision(self, run_id, day, book, coin, current_q, target_q, traded, detail=None):
        self.db.execute(
            "INSERT INTO decisions(run_id, day, book, coin, current_q, target_q, traded, detail) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, str(day), book, coin, current_q, target_q, int(bool(traded)), json.dumps(detail or {})),
        )

    def add_order(self, run_id, day, purpose, coin, market, side, size, mid, status, reason=None, pair=None):
        cur = self.db.execute(
            "INSERT INTO orders(run_id, ts, mode, network, day, purpose, coin, market, side, size, decision_mid, "
            "notional, pair, status, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, time.time(), self.mode, self.network, str(day), purpose, coin, market, side, size, mid,
             abs(size) * mid if mid else None, pair, status, reason),
        )
        return cur.lastrowid

    def finish_order(self, order_id, status, filled, avg_px, fee, slippage_bps, reason=None):
        self.db.execute(
            "UPDATE orders SET status=?, filled=?, avg_px=?, fee=?, slippage_bps=?, reason=COALESCE(?, reason) "
            "WHERE id=?",
            (status, filled, avg_px, fee, slippage_bps, reason, order_id),
        )

    def add_attempt(self, order_id, attempt, cloid, size, limit_px, mid, result):
        raw = result.raw
        try:
            raw = json.dumps(raw)
        except (TypeError, ValueError):
            raw = str(raw)
        self.db.execute(
            "INSERT INTO attempts(order_id, ts, attempt, cloid, size, limit_px, mid, status, filled, avg_px, fee, oid, "
            "error, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, time.time(), attempt, cloid, size, limit_px, mid, result.status, result.filled, result.avg_px,
             result.fee, result.oid, result.error, raw),
        )

    def traded_notional_on(self, day):
        row = self.db.execute(
            "SELECT COALESCE(SUM(a.filled * COALESCE(a.avg_px, a.mid)), 0) AS n FROM attempts a "
            "JOIN orders o ON o.id = a.order_id WHERE o.day=? AND o.mode=? AND o.network=?",
            (str(day), self.mode, self.network),
        ).fetchone()
        return float(row["n"])

    def orders_for_run(self, run_id):
        return self.db.execute("SELECT * FROM orders WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    # ---- funding, equity, guard, alerts

    def add_funding(self, time_ms, coin, usdc, szi, rate, source, hash_=None):
        self.db.execute(
            "INSERT OR IGNORE INTO funding(mode, network, time_ms, coin, usdc, szi, rate, source, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.mode, self.network, int(time_ms), coin, usdc, szi, rate, source, hash_),
        )

    def add_venue_fill(self, time_ms, coin, market, venue_coin, side, px, sz, fee, fee_token, closed_pnl, dir_,
                       hash_, oid, tid, cloid=None, raw=None):
        self.db.execute(
            "INSERT OR IGNORE INTO venue_fills(mode, network, time_ms, coin, market, venue_coin, side, px, sz, fee, "
            "fee_token, closed_pnl, dir, hash, oid, tid, cloid, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.mode, self.network, int(time_ms), coin, market, venue_coin, side, px, sz, fee, fee_token,
             closed_pnl, dir_, hash_, None if oid is None else str(oid), str(tid), cloid,
             json.dumps(raw) if raw is not None else None),
        )

    def add_venue_ledger(self, time_ms, kind, usdc, ref, hash_=None, token=None, amount=None, direction=None,
                         raw=None):
        self.db.execute(
            "INSERT OR IGNORE INTO venue_ledger(mode, network, time_ms, kind, usdc, token, amount, direction, hash, "
            "ref, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.mode, self.network, int(time_ms), kind, usdc, token, amount, direction, hash_, str(ref),
             json.dumps(raw) if raw is not None else None),
        )

    def add_peg_gap(self, coin, spot_mid, perp_mark, gap, ts=None):
        self.db.execute(
            "INSERT INTO peg_gaps(ts, mode, network, coin, spot_mid, perp_mark, gap) VALUES (?,?,?,?,?,?,?)",
            (ts or time.time(), self.mode, self.network, coin, spot_mid, perp_mark, gap),
        )

    def peg_summary(self, start_ts, end_ts):
        rows = self.db.execute(
            "SELECT coin, COUNT(*) AS n, MIN(gap) AS lo, MAX(gap) AS hi, AVG(gap) AS mean, MAX(ABS(gap)) AS worst "
            "FROM peg_gaps WHERE mode=? AND network=? AND ts>=? AND ts<? GROUP BY coin",
            (self.mode, self.network, start_ts, end_ts),
        ).fetchall()
        return {r["coin"]: {"observations": r["n"], "min": r["lo"], "max": r["hi"], "mean": r["mean"],
                            "worst_abs": r["worst"]} for r in rows}

    def funding_between(self, start_ms, end_ms):
        return self.db.execute(
            "SELECT * FROM funding WHERE mode=? AND network=? AND time_ms>? AND time_ms<=? ORDER BY time_ms",
            (self.mode, self.network, int(start_ms), int(end_ms)),
        ).fetchall()

    def put_equity(self, day, book, equity, detail=None):
        self.db.execute(
            "INSERT INTO equity(mode, network, day, book, equity, detail) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(mode, network, day, book) DO UPDATE SET equity=excluded.equity, detail=excluded.detail",
            (self.mode, self.network, str(day), book, float(equity), json.dumps(detail or {})),
        )

    def equity_series(self, book):
        return self.db.execute(
            "SELECT day, equity, detail FROM equity WHERE mode=? AND network=? AND book=? ORDER BY day",
            (self.mode, self.network, book),
        ).fetchall()

    def add_guard_action(self, action, coin=None, amount=None, ok=True, detail=None):
        self.db.execute(
            "INSERT INTO guard_actions(ts, mode, network, action, coin, amount, ok, detail) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), self.mode, self.network, action, coin, amount, int(bool(ok)), json.dumps(detail or {})),
        )

    def add_alert(self, key, lane, summary, act, exit_code, detail=None):
        self.db.execute(
            "INSERT INTO alerts(ts, key, lane, summary, act, exit_code, detail, mode, network) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), key, lane, summary, act, exit_code, json.dumps(detail or {}), self.mode, self.network),
        )

    def add_selftest_step(self, session, step, name, ok, detail=None):
        self.db.execute(
            "INSERT INTO selftest_steps(session, ts, mode, network, step, name, ok, detail) VALUES (?,?,?,?,?,?,?,?)",
            (session, time.time(), self.mode, self.network, step, name, int(bool(ok)),
             json.dumps(detail or {}, default=str)),
        )

    def selftest_steps(self, session=None):
        q = "SELECT * FROM selftest_steps WHERE mode=? AND network=?"
        args = [self.mode, self.network]
        if session:
            q += " AND session=?"
            args.append(session)
        return self.db.execute(q + " ORDER BY id", args).fetchall()

    def alerted_since(self, key, since_ts):
        row = self.db.execute("SELECT 1 FROM alerts WHERE key=? AND ts>=? AND (exit_code IS NULL OR exit_code=0) "
                              "AND mode=? AND network=?", (key, since_ts, self.mode, self.network)).fetchone()
        return row is not None

    # ---- every command the bot ran: when it was due, when it started and finished, what came of it

    def add_chat_request(self, doc):
        """One request through the chat door and its outcome, from the relay's log; once each. Returns 1 if new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO chat_requests(request, at, mode, network, kind, strategy, fingerprint, outcome, said) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(doc["request"]), float(doc["at"]), doc.get("mode"), doc.get("network"), doc.get("kind"),
             doc.get("strategy"), doc.get("fingerprint"), str(doc["outcome"]), doc.get("said")))
        return cur.rowcount

    def chat_requests(self, limit=20):
        return self.db.execute("SELECT * FROM chat_requests ORDER BY at DESC LIMIT ?", (limit,)).fetchall()

    def start_invocation(self, command, unit, every_s, scheduled_at, proc_start, main_start, pid):
        cur = self.db.execute(
            "INSERT INTO invocations(mode, network, command, unit, every_s, scheduled_at, proc_start, main_start, pid) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.mode, self.network, command, unit, every_s, scheduled_at, proc_start, main_start, pid))
        return cur.lastrowid

    def finish_invocation(self, inv_id, exit_code, outcome=None, error=None, retries=None):
        self.db.execute(
            "UPDATE invocations SET finished_at=?, exit_code=?, outcome=?, error=?, retries=? WHERE id=?",
            (time.time(), exit_code, json.dumps(outcome, default=str) if outcome is not None else None, error,
             json.dumps(retries, default=str) if retries else None, inv_id))
