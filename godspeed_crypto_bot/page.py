"""The page: one HTML file, in plain words, at a link that never changes, from the bot's own records.

It shows the account against its starting money, a chart of the account and of what the backtest would have
made on the same days, what the account holds now and which half of the strategy each holding belongs to,
every trade (newest first) with the balance after it, funding and fees per day, the safety check's actions
and every alert, and a short section for the practice exchange (testnet).

Everything comes from bot.sqlite, opened read-only; nothing here touches the network or a key. The page
names no wallet or API address. It is written to PAGE_DIR and served behind the owner's Godspeed sign-in, never on a
public link (godspeed.py: a path file in Caddy's add-on folder, or a Caddy container of its own on Docker).
The daily run, flatten, halt and resume refresh it, and so does a guard run that traded, moved money or
alerted (cli.py).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
from .names import DISPLAY_NAME
from .names import PAGE_DIR as PAGE_DIR_NAME
from .strategy import STRATEGY_WORDS

log = logging.getLogger(__name__)

PAGE_DIR = Path(PAGE_DIR_NAME)
PAGE_NAME = "index.html"  # the folder is served as it is, so the address alone opens it
TESTNET_CONFIG = config.DEFAULT_CONFIG.with_name("config.testnet.toml")

NAMES = {"BTC": "Bitcoin", "ETH": "Ether", "SOL": "Solana", "HYPE": "Hype", "PURR": "Purr"}
SPOT_TOKENS = {"UBTC": "BTC", "UETH": "ETH", "USOL": "SOL"}
EPS = 1e-9

# Times are UTC, and beside them the server's own time when the server does not keep UTC (the owner sets it with
# `timedatectl set-timezone`). The hours come from the system's own clock settings, read the way every program on
# the server reads them (TZ, else /etc/localtime); only the place's name is looked for, else it says server time.
IANA_NAME = re.compile(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+")


def zone_name(tz_env="", localtime_target=""):
    """The place an IANA zone name gives ("America/New_York" gives "New York"), from TZ when it is such a name,
    else from where /etc/localtime links to; "" when neither names one (a POSIX rule, a copied file)."""
    tz = (tz_env or "").strip().lstrip(":")
    key = tz if IANA_NAME.fullmatch(tz) else ""
    if not tz and "zoneinfo/" in (localtime_target or ""):
        key = localtime_target.split("zoneinfo/", 1)[1].removeprefix("posix/").removeprefix("right/")
        key = key if IANA_NAME.fullmatch(key) else ""
    return key.rsplit("/", 1)[-1].replace("_", " ") if key else ""


def server_zone(env=os.environ, localtime="/etc/localtime", offsets=None):
    """None when the server keeps UTC, else (None, words after a time, words for the footer); None as the zone
    means the system's own local time. `offsets` (standard, summer) in seconds west of UTC, for the tests."""
    if offsets is None:
        if hasattr(time, "tzset"):  # read TZ and /etc/localtime afresh (not on Windows)
            time.tzset()
        offsets = (time.timezone, time.altzone if time.daylight else time.timezone)
    if offsets == (0, 0):
        return None
    tz = (env.get("TZ") or "").strip().lstrip(":")
    if os.path.isabs(tz):  # TZ names a file (TZ=:/etc/localtime): its name is looked for where it links to
        localtime, tz = tz, ""
    try:
        target = Path(os.path.realpath(localtime)).as_posix() if not tz and os.path.lexists(localtime) else ""
    except OSError:
        target = ""
    place = zone_name(tz, target)
    return (None, f"in {place}", f"{place} time") if place else (None, "server time", "the server's time")


ZONE = server_zone()


# ---- reading the records


def _connect(path):
    p = Path(path)
    if not p.exists():
        return None
    db = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _json(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


@dataclass
class Trade:
    ts: float
    coin: str
    market: str  # perp | spot
    side: str  # buy | sell
    size: float
    price: float
    fee: float  # USDC
    purpose: str
    pair: str | None
    realized: float = 0.0
    before: float = 0.0  # position in this coin and market before the trade
    after: float = 0.0
    balance_after: float | None = None
    token: str | None = None


@dataclass
class Track:
    cfg: config.Config
    label: str
    has_records: bool = False
    start: dict | None = None
    account: list = field(default_factory=list)  # (day, value, at)
    backtest: list = field(default_factory=list)  # (day, value)
    trades: list = field(default_factory=list)
    funding_days: list = field(default_factory=list)  # (day, funding usdc, fees usdc, trades)
    guard: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    holdings: list = field(default_factory=list)
    cash: float | None = None
    holdings_at: float | None = None
    halted: str | None = None
    last_run: dict | None = None
    halted_runs: list = field(default_factory=list)


def _fills(db, mode, network, since_ms):
    """Every fill since the start, joined to the order it belongs to (its purpose, coin, carry pair)."""
    rows = db.execute(
        "SELECT f.time_ms, f.coin, f.market, f.venue_coin, f.side, f.px, f.sz, f.fee, f.fee_token, f.tid, "
        "o.id AS order_id, o.coin AS order_coin, o.purpose, o.pair FROM venue_fills f "
        "LEFT JOIN attempts a ON a.cloid = f.cloid AND f.cloid IS NOT NULL "
        "LEFT JOIN orders o ON o.id = a.order_id AND o.mode = f.mode AND o.network = f.network "
        "WHERE f.mode=? AND f.network=? AND f.time_ms>=? ORDER BY f.time_ms, f.id",
        (mode, network, since_ms)).fetchall()
    seen, out = set(), []
    for r in rows:  # an order retried with the same cloid joins once per attempt: keep each fill once
        if r["tid"] in seen:
            continue
        seen.add(r["tid"])
        out.append(r)
    return out


def _prices(db, mode, network):
    """(ts, coin, perp mark, spot mid) from the guard's five-minute peg observations, oldest first."""
    return [(r["ts"], r["coin"], r["perp_mark"], r["spot_mid"]) for r in db.execute(
        "SELECT ts, coin, perp_mark, spot_mid FROM peg_gaps WHERE mode=? AND network=? ORDER BY ts",
        (mode, network))]


def replay(start_value, fills, funding, prices):
    """The account rebuilt trade by trade: what each trade realized and the account's value right after it.

    Cash starts at the starting value; a perp fill realizes profit on the part it closes (average entry
    price), a spot fill pays or receives cash; fees and funding come off or go on cash. The value after a
    trade is cash plus every open perp's profit so far plus the spot coins, each at the latest price known
    then (the trade's own fill, else the guard's last observation). Returns (trades, state).
    """
    cash = float(start_value)
    perp, spot = {}, {}  # coin -> [size, entry]
    last_px = {}  # (coin, market) -> price
    events = [(f["time_ms"] / 1000, 0, f) for f in fills] + [(t / 1000, 1, (c, u)) for t, c, u in funding]
    events.sort(key=lambda e: (e[0], e[1]))
    pi = 0
    trades, by_order = [], {}

    def value():
        v = cash
        for c, (q, e) in perp.items():
            v += q * (last_px.get((c, "perp"), e) - e)
        for c, (q, e) in spot.items():
            v += q * last_px.get((c, "spot"), e)
        return v

    for ts, kind, item in events:
        while pi < len(prices) and prices[pi][0] <= ts:
            _t, c, mark, mid = prices[pi]
            last_px[(c, "perp")] = mark
            last_px[(c, "spot")] = mid
            pi += 1
        if kind == 1:
            cash += item[1]
            continue
        f = item
        market = f["market"]
        coin = f["order_coin"] or f["coin"]
        if market == "spot" and coin.startswith("@"):
            coin = f["venue_coin"] or coin
        coin = SPOT_TOKENS.get(coin, coin)
        px, sz = float(f["px"]), float(f["sz"])
        dq = sz if f["side"] == "buy" else -sz
        fee = float(f["fee"] or 0.0)
        fee_token = f["fee_token"] or "USDC"
        fee_usdc = fee if fee_token == "USDC" else fee * px  # Hyperliquid charges a spot buy's fee in the coin
        book = perp if market == "perp" else spot
        q, entry = book.get(coin, [0.0, px])
        before = q
        realized = 0.0
        if q and (q > 0) != (dq > 0):
            closed = min(abs(dq), abs(q))
            realized = closed * (px - entry) * (1 if q > 0 else -1)
        new = q + dq
        if abs(new) < EPS:
            new = 0.0
        if q == 0 or (q > 0) == (dq > 0):
            entry = (entry * abs(q) + px * abs(dq)) / abs(new) if new else px
        elif abs(dq) > abs(q):
            entry = px
        if market == "spot" and fee_token != "USDC":
            new -= fee
        if market == "perp":
            cash += realized - fee_usdc
        else:
            cash += -dq * px - (fee_usdc if fee_token == "USDC" else 0.0)
        if new:
            book[coin] = [new, entry]
        else:
            book.pop(coin, None)
        last_px[(coin, market)] = px
        key = f["order_id"] if f["order_id"] is not None else f"fill-{f['tid']}"
        t = by_order.get(key)
        if t is None:
            t = Trade(ts, coin, market, f["side"], 0.0, 0.0, 0.0, f["purpose"] or "outside the bot", f["pair"],
                      before=before, token=f["venue_coin"] if market == "spot" else None)
            by_order[key] = t
            trades.append(t)
        t.price = (t.price * t.size + px * sz) / (t.size + sz)
        t.size += sz
        t.fee += fee_usdc
        t.realized += realized
        t.after = new
        t.balance_after = value()
    for _t, c, mark, mid in prices[pi:]:  # the latest prices known, for what it holds now
        last_px[(c, "perp")] = mark
        last_px[(c, "spot")] = mid
    return trades, {"cash": cash, "perp": perp, "spot": spot, "prices": last_px, "value": value(),
                    "prices_at": prices[-1][0] if prices else None}


def load_track(cfg, label):
    tr = Track(cfg, label)
    if cfg.halt_path.exists():
        try:
            tr.halted = cfg.halt_path.read_text(encoding="utf-8").strip() or "no reason given"
        except OSError:
            tr.halted = "a stop file is in place"
    db = _connect(cfg.db_path)
    if db is None:
        return tr
    try:
        m, n = cfg.mode, cfg.network
        row = db.execute("SELECT value FROM state WHERE mode=? AND network=? AND name='start'", (m, n)).fetchone()
        tr.start = _json(row["value"]) if row else None
        if not tr.start:
            return tr
        tr.has_records = True
        for r in db.execute("SELECT day, equity, detail FROM equity WHERE mode=? AND network=? AND book='account' "
                            "ORDER BY day", (m, n)):
            at = (_json(r["detail"], {}) or {}).get("at")
            tr.account.append((r["day"], r["equity"], at))
        tr.backtest = [(r["day"], r["equity"]) for r in db.execute(
            "SELECT day, equity FROM equity WHERE mode=? AND network=? AND book='shadow_total' ORDER BY day", (m, n))]
        since = int(tr.start.get("ts_ms") or 0)
        funding = [(r["time_ms"], r["coin"], r["usdc"]) for r in db.execute(
            "SELECT time_ms, coin, usdc FROM funding WHERE mode=? AND network=? AND time_ms>=? ORDER BY time_ms",
            (m, n, since))]
        prices = _prices(db, m, n)
        trades, st = replay(tr.start.get("account_value", cfg.capital_cap_usdc), _fills(db, m, n, since), funding,
                            prices)
        tr.trades = trades
        days = {}
        for t_ms, _c, usdc in funding:
            d = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).date().isoformat()
            days.setdefault(d, [0.0, 0.0, 0])[0] += usdc
        for t in trades:
            d = datetime.fromtimestamp(t.ts, tz=timezone.utc).date().isoformat()
            e = days.setdefault(d, [0.0, 0.0, 0])
            e[1] += t.fee
            e[2] += 1
        tr.funding_days = [(d, *v) for d, v in sorted(days.items())]
        start_s = since / 1000
        tr.guard = [dict(r) for r in db.execute(
            "SELECT ts, action, coin, amount, ok, detail FROM guard_actions WHERE mode=? AND network=? AND ts>=? "
            "ORDER BY ts DESC", (m, n, start_s - 60))]
        tr.alerts = [dict(r) for r in db.execute(
            "SELECT ts, lane, summary, act FROM alerts WHERE mode=? AND network=? AND ts>=? ORDER BY ts DESC",
            (m, n, start_s - 60))]
        run = db.execute("SELECT day, status, finished_at, error FROM runs WHERE kind='daily' AND mode=? AND "
                         "network=? ORDER BY day DESC LIMIT 1", (m, n)).fetchone()
        tr.last_run = dict(run) if run else None
        tr.halted_runs = [dict(r) for r in db.execute(
            "SELECT day, status, error FROM runs WHERE kind='daily' AND mode=? AND network=? AND day>=? AND "
            "status IN ('halted', 'failed', 'skipped') ORDER BY day DESC", (m, n, tr.start.get("day", "")))]
        _holdings(db, tr, st)
    finally:
        db.close()
    return tr


def _holdings(db, tr, st):
    m, n = tr.cfg.mode, tr.cfg.network
    row = db.execute("SELECT value FROM state WHERE mode=? AND network=? AND name='expected_holdings'",
                     (m, n)).fetchone()
    expected = (_json(row["value"], {}) if row else {}) or {}
    tr.holdings_at = st.get("prices_at") or expected.get("ts")
    books_row = db.execute("SELECT state FROM books WHERE mode=? AND network=? ORDER BY day DESC LIMIT 1",
                           (m, n)).fetchone()
    books = ((_json(books_row["state"], {}) if books_row else {}) or {}).get("books", {})
    trend_q = books.get("trend", {}).get("q", {})
    carry_perp = books.get("carry", {}).get("perp_q", {})
    carry_spot = books.get("carry", {}).get("spot_q", {})
    sizes = {}
    for coin, legs in (expected.get("coins") or {}).items():
        for market in ("perp", "spot"):
            q = float(legs.get(market) or 0.0)
            if abs(q) > EPS:
                sizes[(coin, market)] = q
    if not expected:  # never snapshotted: what the trades add up to
        for coin, (q, _e) in st["perp"].items():
            sizes[(coin, "perp")] = q
        for coin, (q, _e) in st["spot"].items():
            sizes[(coin, "spot")] = q
    px = st["prices"]
    out = []
    unreal = spot_value = 0.0
    for (coin, market), q in sorted(sizes.items()):
        p = px.get((coin, market))
        entry = (st["perp"] if market == "perp" else st["spot"]).get(coin, [None, None])[1]
        h = {"coin": coin, "market": market, "size": q, "price": p, "entry": entry,
             "trend": float(trend_q.get(coin, 0.0)) if market == "perp" else 0.0,
             "carry": float((carry_perp if market == "perp" else carry_spot).get(coin, 0.0))}
        if p is not None:
            h["worth"] = abs(q) * p
            if market == "perp" and entry is not None:
                h["profit"] = q * (p - entry)
                unreal += h["profit"]
            elif market == "spot":
                spot_value += q * p
        out.append(h)
    tr.holdings = out
    tr.cash = st["value"] - unreal - spot_value


# ---- words and numbers


def _esc(x):
    return html.escape(str(x), quote=True)


def usd(x, sign=False):
    if x is None:
        return "unknown"
    s = f"{abs(x):,.2f}"
    if sign:
        return ("+" if x >= 0 else "-") + "$" + s
    return ("-" if x < 0 else "") + "$" + s


def usdc(x):
    return "unknown" if x is None else f"{x:,.2f} USDC"


def pct(x):
    return "unknown" if x is None else f"{x * 100:+.2f}%"


def qty(x):
    s = f"{abs(x):.6f}".rstrip("0").rstrip(".")
    return s or "0"


def price(x):
    if x is None:
        return "unknown"
    return f"${x:,.2f}" if x >= 1 else f"${x:,.6f}"


def when(ts):
    if ts is None:
        return "unknown"
    t = datetime.fromtimestamp(ts, tz=timezone.utc)
    s = f"{t.day} {t:%b %Y}, {t:%H:%M} UTC"
    if ZONE is not None:
        local = t.astimezone(ZONE[0]) if ZONE[0] is not None else t.astimezone()  # the system's own local time
        s += f" ({local:%H:%M} {ZONE[1]})"
    return s


def times_note():
    return f"All times are UTC, with {ZONE[2]} in brackets." if ZONE is not None else "All times are UTC."


def short_day(d):
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.day} {d:%b}"


def name(coin):
    return NAMES.get(coin, coin)


def mode_words(cfg, start_value=None):
    if cfg.network == "testnet":
        return ("Practice exchange", "Play money on Hyperliquid's practice exchange (testnet). Its prices are "
                "close to the real ones for Bitcoin and Ether perpetual contracts, but none of it is real money.")
    if cfg.mode == "paper":
        return ("Pretend money on real prices", f"The bot trades a pretend account of {start_value or cfg.capital_cap_usdc:,.0f} USDC against "
                "Hyperliquid's real order book and books the real hourly funding. No real money is involved yet.")
    if cfg.mode == "live":
        return ("Real money", "The bot trades real money on Hyperliquid.")
    return ("Rehearsal", "A rehearsal: every order is imagined at the market price. No money and no exchange "
            "account are involved.")


def trade_sentence(t):
    n = name(t.coin)
    if t.market == "spot":
        held = f" (held as the token {t.token})" if t.token and not t.token.startswith("@") else ""
        if t.side == "buy":
            s = f"Bought {qty(t.size)} {t.coin} of real {n}{held}"
        else:
            s = f"Sold {qty(t.size)} {t.coin} of {n}{held}"
        if t.pair and t.pair.endswith("-pair"):
            s += ", one half of the carry trade: the coin is held while an equal bet on its price falling " \
                 "cancels out the price moves, and the bot collects the funding that the bet earns"
        return s + "."
    b, a = t.before, t.after
    verb = "Bought" if t.side == "buy" else "Sold"
    if abs(b) < EPS:
        what = f"opened a bet on {n} {'rising' if a > 0 else 'falling'}"
    elif abs(a) < EPS:
        what = f"closed the bet on {n} {'rising' if b > 0 else 'falling'}"
    elif (a > 0) != (b > 0):
        what = (f"closed the bet on {n} {'rising' if b > 0 else 'falling'} and opened a bet on it "
                f"{'rising' if a > 0 else 'falling'}")
    elif abs(a) > abs(b):
        what = f"made the bet on {n} {'rising' if a > 0 else 'falling'} bigger"
    else:
        what = f"made the bet on {n} {'rising' if a > 0 else 'falling'} smaller"
    s = f"{verb} {qty(t.size)} {t.coin} in perpetual contracts: {what}"
    if a and abs(b) >= EPS and abs(a) >= EPS:
        s += f" (now {qty(a)} {t.coin})"
    if t.pair and t.pair.endswith("-pair"):
        s += ". Part of it is the carry trade's bet on falling that offsets the coin bought with it"
    if t.purpose == "flatten":
        s = "Closing everything: " + s[0].lower() + s[1:]
    elif t.purpose == "guard":
        s = "Safety check: " + s[0].lower() + s[1:]
    elif t.purpose == "selftest":
        s = "Self-test trade: " + s[0].lower() + s[1:]
    elif t.purpose == "outside the bot":
        s = "A trade the bot did not place: " + s[0].lower() + s[1:]
    return s + "."


def guard_sentence(g):
    a, amount, coin = g["action"], g["amount"], g["coin"]
    d = _json(g["detail"], {}) or {}
    n = name(coin) if coin else ""
    if a == "transfer_to_spot":
        s = (f"Moved {usdc(amount)} inside the account, from the balance that backs the perpetual contracts to "
             "the balance that buys coins, to pay for a coin purchase.")
    elif a == "transfer_to_perp":
        why = {"sweep after trading": "after trading, the spare cash went back",
               "spot USDC before trading": "before trading, spare cash went over"}.get(d.get("reason"),
                                                                                       "to keep borrowing safe")
        s = f"Moved {usdc(amount)} inside the account to the balance that backs the perpetual contracts ({why})."
    elif a == "flatten":
        s = f"Closed every position and stopped trading. Reason: {d.get('reason', 'not recorded')}."
    elif a == "shrink_pair":
        s = f"Made the {n} carry trade {round((amount or 0) * 100)}% smaller to keep the borrowing at a safe level."
    elif a == "peg_exit":
        s = (f"Closed the {n} carry trade because the token it held stopped tracking the real {n} price "
             f"({(amount or 0) * 100:+.1f}%).")
    elif a == "one_legged_carry":
        s = f"Repaired the {n} carry trade after the exchange closed part of its bet on falling."
    elif a == "holdings_mismatch":
        s = "Stopped trading: the exchange showed holdings the bot could not explain."
    elif a == "unfixed":
        s = "Could not bring the borrowing back to a safe level."
    else:
        s = f"{a.replace('_', ' ').capitalize()}{' ' + n if n else ''}."
    if not g["ok"]:
        s += " It did not fully work."
    return s


# ---- the page


CSS = """
:root{--bg:#f7f7f4;--card:#fff;--ink:#1d1f1e;--muted:#5f6662;--line:#e2e2dc;--accent:#1f6f50;--accent2:#8a6d1d;
--up:#1f7a46;--down:#b23a2e;--warn-bg:#fff4e0;--warn:#8a5a00;--chip:#eef2ee}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121413;--card:#1b1e1c;--ink:#e8ebe9;
--muted:#a3aaa6;--line:#2c312e;--accent:#5cc79a;--accent2:#d9b25a;--up:#5cc79a;--down:#f08070;--warn-bg:#3a2d12;
--warn:#f2c46b;--chip:#232825}}
:root[data-theme="dark"]{--bg:#121413;--card:#1b1e1c;--ink:#e8ebe9;--muted:#a3aaa6;--line:#2c312e;--accent:#5cc79a;
--accent2:#d9b25a;--up:#5cc79a;--down:#f08070;--warn-bg:#3a2d12;--warn:#f2c46b;--chip:#232825}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:46rem;margin:0 auto;padding:20px 16px 48px}
h1{font-size:1.5rem;margin:0 0 4px}
h2{font-size:1.15rem;margin:32px 0 10px}
h3{font-size:1rem;margin:18px 0 8px}
p{margin:0 0 10px}
.muted{color:var(--muted)}
.small{font-size:.875rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px}
.badge{display:inline-block;background:var(--chip);border-radius:999px;padding:2px 10px;font-size:.8rem;
font-weight:600;color:var(--accent)}
.big{font-size:2.1rem;font-weight:700;letter-spacing:-.01em;line-height:1.15;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:10px 14px;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:4px 16px;margin-top:8px}
.grid div{font-variant-numeric:tabular-nums}
.grid b{display:block;font-size:.75rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 4px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
tfoot td{font-weight:700;border-bottom:0}
svg{display:block;width:100%;height:auto}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:.875rem;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}
details summary{cursor:pointer;color:var(--accent)}
footer{margin-top:36px;color:var(--muted);font-size:.85rem}
"""


def _change(value, start):
    if value is None or not start:
        return None, None
    return value - start, value / start - 1


def headline(tr):
    start = tr.start or {}
    title, explanation = mode_words(tr.cfg, start.get("account_value"))
    start_value = start.get("account_value")
    parts = [f'<p><span class="badge">{_esc(title)}</span></p>', f'<p class="muted small">{_esc(explanation)}</p>',
             f'<p class="small">Strategy: <b>{_esc(STRATEGY_WORDS[tr.cfg.strategy])}</b>.</p>']
    if tr.halted:
        parts.append(f'<div class="warn"><b>Trading is stopped.</b> {_esc(tr.halted)}</div>')
    if not tr.account:
        parts.append('<div class="card"><p>No day has closed yet since the start.</p></div>')
        return "".join(parts)
    day, value, at = tr.account[-1]
    diff, rel = _change(value, start_value)
    cls = "up" if (diff or 0) >= 0 else "down"
    at_ts = datetime.fromisoformat(at).timestamp() if at else None
    lines = [
        '<div class="card">',
        '<p class="muted small">Account value</p>',
        f'<p class="big">{_esc(usdc(value))}</p>',
        f'<p class="{cls}"><b>{_esc(usd(diff, sign=True))} ({_esc(pct(rel))})</b> since the start with '
        f'{_esc(usdc(start_value))} on {_esc(short_day(start.get("day", day)))}</p>',
        f'<p class="muted small">At the close of {_esc(short_day(day))}, counted {_esc(when(at_ts))}.</p>',
    ]
    bt = dict(tr.backtest)
    if day in bt:
        b0 = tr.backtest[0][1] if tr.backtest else None
        bdiff, brel = _change(bt[day], b0)
        lines.append(f'<p class="small">The backtest, on the same days with the same decisions, would have: '
                     f'<b>{_esc(usdc(bt[day]))}</b> ({_esc(pct(brel))}).</p>')
    lines.append("</div>")
    parts += lines
    run = tr.last_run
    if run:
        status = {"complete": "finished normally", "halted": "stopped without trading", "failed": "failed",
                  "skipped": "was skipped (it started too late)"}.get(run["status"], run["status"])
        parts.append(f'<p class="small muted">Last daily run: {_esc(short_day(run["day"]))}, {_esc(status)}'
                     f'{", " + _esc(when(run["finished_at"])) if run.get("finished_at") else ""}.</p>')
    return "".join(parts)


def chart(tr):
    pts = [(d, v) for d, v, _ in tr.account]
    bt = [(d, v) for d, v in tr.backtest if not pts or d >= pts[0][0]]
    days = sorted({d for d, _ in pts} | {d for d, _ in bt})
    if len(days) < 2:
        return '<p class="muted">The chart starts once two days have closed.</p>'
    vals = [v for _, v in pts] + [v for _, v in bt]
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.15, 1.0)
    lo, hi = lo - pad, hi + pad
    w, h, left, right, top, bottom = 480, 250, 54, 10, 12, 32
    x0 = date.fromisoformat(days[0])
    span = max((date.fromisoformat(days[-1]) - x0).days, 1)

    def xy(d, v):
        x = left + (date.fromisoformat(d) - x0).days / span * (w - left - right)
        y = top + (hi - v) / (hi - lo) * (h - top - bottom)
        return f"{x:.1f},{y:.1f}"

    grid = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = top + (hi - v) / (hi - lo) * (h - top - bottom)
        grid.append(f'<line x1="{left}" x2="{w - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>'
                    f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="13" '
                    f'fill="var(--muted)">{v:,.0f}</text>')
    step = max(1, len(days) // 6)
    for i, d in enumerate(days):
        if i % step and i != len(days) - 1:
            continue
        x = xy(d, lo).split(",")[0]
        anchor = "start" if i == 0 else ("end" if i == len(days) - 1 else "middle")
        grid.append(f'<text x="{x}" y="{h - 8}" text-anchor="{anchor}" font-size="13" '
                    f'fill="var(--muted)">{_esc(short_day(d))}</text>')
    start = (tr.start or {}).get("account_value")
    if start and lo < start < hi:
        y = top + (hi - start) / (hi - lo) * (h - top - bottom)
        grid.append(f'<line x1="{left}" x2="{w - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--muted)" '
                    'stroke-dasharray="2 4"/>')

    def line(series, color, dash=""):
        if not series:
            return ""
        p = " ".join(xy(d, v) for d, v in series)
        dots = "".join(f'<circle cx="{xy(d, v).split(",")[0]}" cy="{xy(d, v).split(",")[1]}" r="3" fill="{color}">'
                       f'<title>{_esc(short_day(d))}: {_esc(usdc(v))}</title></circle>' for d, v in series)
        return (f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" '
                f'{dash}/>{dots}')

    dashed = 'stroke-dasharray="6 5"'
    svg = (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Account value per day and what the backtest '
           f'would have made">{"".join(grid)}{line(bt, "var(--accent2)", dashed)}'
           f'{line(pts, "var(--accent)")}</svg>')
    legend = ('<div class="legend"><span><i style="background:var(--accent)"></i>The account</span>'
              '<span><i style="background:var(--accent2)"></i>What the backtest would have made</span>'
              '<span><i style="background:var(--muted)"></i>Starting money (dotted)</span></div>')
    return f'<div class="card">{svg}{legend}</div>'


def holdings(tr):
    if not tr.holdings:
        return '<div class="card"><p>It holds nothing but cash right now.</p></div>'
    rows = []
    for h in tr.holdings:
        n = name(h["coin"])
        if h["market"] == "spot":
            what = f"Real {n} (the coin itself), {qty(h['size'])} {h['coin']}"
            half = "The carry half: held so its price moves cancel out the bet on falling below."
        else:
            what = f"Bet on {n} {'rising' if h['size'] > 0 else 'falling'} (perpetual contract), " \
                   f"{qty(h['size'])} {h['coin']}"
            parts = []
            if abs(h["trend"]) > EPS:
                parts.append(f"the trend half wants a bet on {'rising' if h['trend'] > 0 else 'falling'} of "
                             f"{qty(h['trend'])} {h['coin']}")
            if abs(h["carry"]) > EPS:
                parts.append(f"the carry half a bet on falling of {qty(h['carry'])} {h['coin']}, the other side of "
                             f"the {n} it holds")
            if parts:
                half = "One position for both halves: " + " and ".join(parts) + "."
                if len(parts) == 1:
                    half = ("The trend half: it" + parts[0][len("the trend half"):] if abs(h["trend"]) > EPS
                            else "The carry half: it" + parts[0][len("the carry half"):]) + "."
            else:
                half = "Neither half asks for this any more; the next daily run closes it."
        grid = [f"<div><b>Worth</b>{_esc(usd(h.get('worth')))}</div>",
                f"<div><b>Price now</b>{_esc(price(h.get('price')))}</div>"]
        if "profit" in h:
            cls = "up" if h["profit"] >= 0 else "down"
            grid.append(f'<div><b>Profit so far</b><span class="{cls}">{_esc(usd(h["profit"], sign=True))}</span></div>')
        if tr.cfg.strategy != "mix":  # one strategy runs: there are no halves
            half = (half.replace("The trend half", "The trend strategy").replace("The carry half", "The carry strategy")
                    .replace("Neither half asks for this any more", "The strategy no longer asks for this"))
        rows.append(f'<div class="card"><p><b>{_esc(what)}</b></p><p class="small muted">{_esc(half)}</p>'
                    f'<div class="grid">{"".join(grid)}</div></div>')
    rows.append(f'<p class="small">Cash: about {_esc(usdc(tr.cash))}. Prices from '
                f'{_esc(when(tr.holdings_at))}.</p>')
    return "".join(rows)


def trades(tr, limit=None):
    if not tr.trades:
        return '<p class="muted">No trades yet.</p>'
    out = []
    items = list(reversed(tr.trades))
    for t in items[:limit] if limit else items:
        cls = "up" if t.realized >= 0 else "down"
        realized = (f'<span class="{cls}">{_esc(usd(t.realized, sign=True))}</span>'
                    if abs(t.realized) >= 0.005 else "none (nothing was closed)")
        out.append(
            f'<div class="card"><p>{_esc(trade_sentence(t))}</p><div class="grid">'
            f'<div><b>When</b>{_esc(when(t.ts))}</div>'
            f'<div><b>Amount</b>{_esc(qty(t.size))} {_esc(t.coin)}, {_esc(usd(t.size * t.price))}</div>'
            f'<div><b>Price</b>{_esc(price(t.price))}</div>'
            f'<div><b>Fee</b>{_esc(usd(t.fee))}</div>'
            f'<div><b>Profit realised (before the fee)</b>{realized}</div>'
            f'<div><b>Balance after</b>{_esc(usdc(t.balance_after))}</div></div></div>')
    return "".join(out)


def funding_table(tr):
    if not tr.funding_days:
        return '<p class="muted">Nothing paid or received yet.</p>'
    rows, tf, tfee = [], 0.0, 0.0
    for d, f, fee, _n in reversed(tr.funding_days):
        tf += f
        tfee += fee
        rows.append(f"<tr><td>{_esc(short_day(d))}</td><td>{_esc(usd(f, sign=True))}</td>"
                    f"<td>{_esc(usd(-fee, sign=True) if fee else '$0.00')}</td>"
                    f"<td>{_esc(usd(f - fee, sign=True))}</td></tr>")
    return ('<div class="card"><table><thead><tr><th>Day (UTC)</th><th>Funding</th><th>Trading fees</th>'
            f'<th>Together</th></tr></thead><tbody>{"".join(rows)}</tbody><tfoot><tr><td>Total</td>'
            f'<td>{_esc(usd(tf, sign=True))}</td><td>{_esc(usd(-tfee, sign=True))}</td>'
            f'<td>{_esc(usd(tf - tfee, sign=True))}</td></tr></tfoot></table></div>')


def safety_section(tr):
    out = []
    if tr.halted:
        out.append(f'<div class="warn"><b>Trading is stopped right now.</b> {_esc(tr.halted)}</div>')
    for r in tr.halted_runs:
        what = {"halted": "did not trade (trading was stopped)", "failed": "failed",
                "skipped": "was skipped because it started too late"}.get(r["status"], r["status"])
        out.append(f'<p>The daily run of {_esc(short_day(r["day"]))} {_esc(what)}.'
                   f'{" " + _esc(r["error"]) if r.get("error") else ""}</p>')
    for a in tr.alerts:
        to = ("sent to you" if a["lane"] in ("owner", "bot")
              else "marked as practice, since no real money was involved")
        out.append(f'<div class="card"><p><b>Alert, {_esc(to)}:</b> the bot {_esc(a["summary"])}.</p>'
                   f'<p class="small muted">{_esc(when(a["ts"]))}. What to do: {_esc(a["act"] or "nothing")}</p>'
                   '</div>')
    if not tr.alerts:
        out.append('<p>No alerts so far.</p>')
    if tr.guard:
        items = "".join(f'<li><span class="muted small">{_esc(when(g["ts"]))}</span><br>{_esc(guard_sentence(g))}'
                        '</li>' for g in tr.guard)
        out.append(f'<h3>What the safety checks did</h3><ul>{items}</ul>')
    else:
        out.append('<p>The safety check, which looks at the account every five minutes, has not needed to do '
                   'anything.</p>')
    return "".join(out)


EXPLAIN = (
    '<details class="card"><summary>What the words mean</summary>'
    '<p class="small"><b>Perpetual contract:</b> a bet on a coin\'s price without owning the coin. A bet on rising '
    'gains when the price goes up, a bet on falling gains when it goes down. The size is in coins: a bet of '
    '0.01 BTC moves like holding 0.01 Bitcoin.</p>'
    '{strategy_paragraph}'
    '<p class="small"><b>Funding:</b> every hour, bets on rising and bets on falling pay each other a small fee. '
    'Plus means the account received it.</p>'
    '<p class="small"><b>The backtest:</b> the same decisions on the same days, but with the textbook assumptions '
    'the strategy was tested with (trades at the day\'s opening price, the expected fees). The gap between the '
    'two lines is what real trading costs or gains compared with the test.</p>'
    '<p class="small"><b>USDC:</b> a digital dollar; 1 USDC is worth about $1.</p></details>'
)


STRATEGY_PARAGRAPHS = {
    "mix": '<p class="small"><b>The two halves.</b> Half the money follows the <b>trend</b>: it bets on Bitcoin and '
           'Ether rising or falling, depending on how they moved over the last one to four months. The other half '
           'is the <b>carry</b> trade: it buys a coin and bets the same amount on it falling, so price moves cancel '
           'out, and it collects the funding that bets on falling earn when most traders bet on rising.</p>',
    "trend": '<p class="small"><b>The trend.</b> All the money follows the <b>trend</b>: it bets on Bitcoin and Ether '
             'rising or falling, depending on how they moved over the last one to four months.</p>',
    "carry": '<p class="small"><b>The carry trade.</b> All the money is in the <b>carry</b> trade: it buys a coin and '
             'bets the same amount on it falling, so price moves cancel out, and it collects the funding that '
             'bets on falling earn when most traders bet on rising.</p>',
}


PRACTICE_NOTES = {
    "mix": 'The practice exchange has no Bitcoin coin to buy, and its Ether and Solana tokens are priced far from '
           'the real market, so the carry half is switched off there and only the trend half trades.',
    "trend": 'The practice exchange runs the trend strategy as it is, since that needs no coins to buy.',
    "carry": 'The practice exchange has no Bitcoin coin to buy, and its Ether and Solana tokens are priced far from '
             'the real market, so the carry trade is switched off there and it mostly holds cash.',
}


def explain(strategy):
    return EXPLAIN.replace("{strategy_paragraph}", STRATEGY_PARAGRAPHS[strategy])


def render(main, practice=None, now=None):
    now = now or datetime.now(timezone.utc)
    body = ['<main>', f'<h1>{_esc(DISPLAY_NAME)}</h1>',
            f'<p class="muted">How {_esc(DISPLAY_NAME)} is doing, refreshed after every daily run '
            '(just after 00:02 UTC) and whenever the safety check trades or moves money.</p>']
    if main is not None:
        body += [headline(main), "<h2>Balance over time</h2>", chart(main), explain(main.cfg.strategy),
                 "<h2>What it holds now</h2>", holdings(main),
                 f"<h2>Every trade ({len(main.trades)}), newest first</h2>", trades(main),
                 "<h2>Funding and fees per day</h2>",
                 '<p class="small muted">Funding: plus means received. Trading fees: what the exchange charged.</p>',
                 funding_table(main), "<h2>Safety checks and alerts</h2>", safety_section(main)]
    if practice is not None:
        body += ['<h2 style="margin-top:44px">The practice exchange</h2>',
                 '<p class="small">The same bot also trades play money on Hyperliquid\'s practice exchange (testnet), '
                 'to prove the real signing and order sending work. ' + PRACTICE_NOTES[practice.cfg.strategy] + '</p>',
                 headline(practice)]
        if practice.trades:
            body += [f"<h3>Its trades ({len(practice.trades)}), newest first</h3>", trades(practice)]
        total_f = sum(f for _d, f, _fee, _n in practice.funding_days)
        total_fee = sum(fee for _d, _f, fee, _n in practice.funding_days)
        if practice.funding_days:
            body.append(f'<p class="small">Funding so far {_esc(usd(total_f, sign=True))}, trading fees '
                        f'{_esc(usd(total_fee))}.</p>')
        if practice.alerts or practice.guard or practice.halted:
            body += ["<h3>Its safety checks and alerts</h3>", safety_section(practice)]
    body.append(f'<footer>Refreshed {_esc(when(now.timestamp()))}. {_esc(times_note())} '
                'Numbers come from the bot\'s own records.</footer></main>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer">'
            f'<title>{_esc(DISPLAY_NAME)}</title>'
            f'<style>{CSS}</style></head><body>{"".join(body)}</body></html>')


def write(out_dir, text):
    """Atomically replace the page (a reader never sees half a file); world-readable for the web server."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".crypto-bot-", suffix=".html", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, out_dir / PAGE_NAME)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out_dir / PAGE_NAME


def tracks(main_cfg=None, practice_cfg=None):
    """The page's two tracks: the main account (paper or live mainnet) and the practice exchange."""
    if main_cfg is None and config.DEFAULT_CONFIG.exists():
        main_cfg = config.load(config.DEFAULT_CONFIG)
    if practice_cfg is None and TESTNET_CONFIG.exists():
        practice_cfg = config.load(TESTNET_CONFIG)
    if main_cfg is not None and main_cfg.network == "testnet" and practice_cfg is None:
        main_cfg, practice_cfg = None, main_cfg
    return main_cfg, practice_cfg


def refresh(main_cfg=None, practice_cfg=None, out_dir=None, now=None):
    m, p = tracks(main_cfg, practice_cfg)
    main = load_track(m, "main") if m is not None else None
    practice = load_track(p, "practice") if p is not None else None
    return write(out_dir or PAGE_DIR, render(main, practice, now))


def url(cfg=None):
    """The page's address, as the relay wrote it (godspeed.py) once Godspeed's web address serves the page."""
    from .godspeed import ADDRESS_FILE

    try:
        return (Path(cfg.relay_dir if cfg else config.RELAY_DIR) / ADDRESS_FILE
                ).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def auto_refresh():
    """Refresh the page if this machine has the page directory and it is writable here; never raises."""
    if not (PAGE_DIR.is_dir() and os.access(PAGE_DIR, os.W_OK)):
        return None
    try:
        return refresh()
    except Exception as exc:  # noqa: BLE001 - the page must never break a trading command
        log.warning("page refresh failed: %s: %s", type(exc).__name__, exc)
        return None


__all__ = ["auto_refresh", "load_track", "refresh", "render", "replay", "url", "write"]
