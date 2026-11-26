"""A person's own strategy: a TOML file of settings, checked against a fixed menu, explained back in plain words, and
turned into target positions with the same inputs and outputs as the built-in books.

A strategy file is data. It is read with tomllib, every key is checked against the menu below and unknown keys are
refused, and the engine only reads numbers from it: nothing in it is executed, imported or passed to a shell. That
keeps a strategy written in a chat from ever reaching the bot's key.

The engine runs a file strategy in the trend book of the existing ``Books`` (perpetuals only), with the carry book
empty, and returns the same ``Decision`` the built-in ``strategy.decide`` returns. So the daily run, the shadow ledger,
the guard, the limits, the page and the records treat it exactly like a built-in. Its arithmetic reuses the built-in
trend book's own functions (``trend_signal``, ``vol_scaled_weights``, ``needs_trade``), in the same operation order:
the built-in trend book written as a strategy file gives the built-in targets bit for bit (tests/test_strategy_files.py).

The format is documented for people in docs/strategy-files.md.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from .strategy import (
    ANNUAL_DAYS,
    MIN_LISTING_DAYS,
    STRATEGIES,
    STRATEGY_WORDS,
    Books,
    CarryBookState,
    CarryConfig,
    Decision,
    TrendBookState,
    _age_ok,
    _decide_carry,
    needs_trade,
    trailing_annualized,
    trend_signal,
    vol_scaled_weights,
)

PREFIX = "file:"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
COIN_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
MAX_FILE_BYTES = 16 * 1024
MAX_DESCRIPTION = 500
MAX_COINS = 10
MAX_GROSS = 2.0  # the most the built-in trend book ever holds; the guard steps in above 2.5 times the money
MAX_PER_COIN = 1.0
MAX_BAND = 0.5
MAX_DAYS = 365  # the bot reads 400 days of history
MAX_FUNDING_DAYS = 30
MAX_PARTS = 5
DEFAULT_BAND = 0.02
# How far a position the no-trade band keeps may sit above the strategy's limits: the built-in trend book's own band,
# which is as far as it ever drifts. A wider band never keeps a coin beyond max_per_coin, or the book beyond
# gross_exposure, by more than this (per coin); for the built-in trend book written as a file it never acts.
LIMIT_DRIFT = 0.02
DEFAULT_GROSS = 1.0
DEFAULT_PER_COIN = 1.0
DEFAULT_COVARIANCE_DAYS = 90

REBALANCE = ("daily", "weekly", "monthly")
DIRECTIONS = ("long-short", "long-only", "short-only")
KINDS = ("momentum", "moving-average-cross", "mean-reversion", "funding", "fixed", "average")
METHODS = ("equal", "volatility")
TOP_KEYS = ("name", "description", "coins", "rebalance", "direction", "no_trade_band", "signal", "sizing")
SIGNAL_KEYS = {
    "momentum": ("lookbacks",),
    "moving-average-cross": ("fast", "slow"),
    "mean-reversion": ("days", "buy_below", "sell_above"),
    "funding": ("days", "min_annual_rate"),
    "fixed": ("weights",),
    "average": ("parts",),
}
SIGNAL_OPTIONAL = {"funding": ("min_annual_rate",)}
SIZING_KEYS = {
    "equal": ("method", "gross_exposure", "max_per_coin"),
    "volatility": ("method", "days", "target_volatility", "covariance_days", "gross_exposure", "max_per_coin"),
    "fixed": ("gross_exposure", "max_per_coin"),
}


class StrategyFileError(ValueError):
    """A strategy file the bot refuses: one plain sentence per problem."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(" ".join(self.problems))


@dataclass(frozen=True)
class Signal:
    kind: str
    lookbacks: tuple = ()
    fast: int = 0
    slow: int = 0
    days: int = 0
    buy_below: float = 0.0
    sell_above: float = 0.0
    min_annual_rate: float = 0.0
    weights: tuple = ()  # ((coin, weight), ...) in the order of coins
    parts: tuple = ()  # ((weight, Signal), ...)


@dataclass(frozen=True)
class Spec:
    name: str
    description: str
    coins: tuple
    rebalance: str
    direction: str
    no_trade_band: float
    signal: Signal
    method: str  # "equal", "volatility" or "fixed" (a fixed signal sets its own sizes)
    volatility_days: int
    target_volatility: float | None
    covariance_days: int
    gross_exposure: float
    max_per_coin: float
    fingerprint: str

    @property
    def setting(self):
        """What the `strategy` setting says for it."""
        return PREFIX + self.name

    @property
    def book_id(self):
        """What the virtual books record as their strategy: a changed file is a new strategy (an immediate
        rebalance, and in live mode a new confirmation)."""
        return f"{PREFIX}{self.name}#{self.fingerprint}"

    def history_days(self):
        """Daily prices the engine needs, beyond the 120 days before a coin is traded."""
        return max(MIN_LISTING_DAYS, _signal_days(self.signal), self.volatility_days,
                   self.covariance_days if self.target_volatility is not None else 0) + 30

    def funding_days(self):
        """Days of funding settlements the engine reads."""
        return max(12, _funding_days(self.signal) + 2)


def _signal_days(s):
    if s.kind == "momentum":
        return max(s.lookbacks)
    if s.kind == "moving-average-cross":
        return s.slow
    if s.kind == "mean-reversion":
        return s.days + 1
    if s.kind == "average":
        return max(_signal_days(p) for _w, p in s.parts)
    return 1


def _funding_days(s):
    if s.kind == "funding":
        return s.days
    if s.kind == "average":
        return max(_funding_days(p) for _w, p in s.parts)
    return 0


# ---- the setting


def is_file_setting(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def setting_name(value) -> str:
    return value[len(PREFIX):]


def check_setting(value):
    """The `strategy` setting: a built-in, or file:<name> naming a file in the bot's strategies folder."""
    if value in STRATEGIES:
        return value
    if is_file_setting(value) and NAME_RE.match(setting_name(value)):
        return value
    raise ValueError('strategy must be "mix", "trend" or "carry", or "file:<name>" for your own strategy file '
                     f"(a name of lowercase letters, digits and dashes), not {value!r}")


def path_for(strategies_dir, name) -> Path:
    if not NAME_RE.match(name or ""):
        raise StrategyFileError([f"{_q(name)} is not a strategy name: use 1 to 40 lowercase letters, digits or "
                                 "dashes, starting with a letter."])
    return Path(strategies_dir) / f"{name}.toml"


# ---- reading and checking


def fingerprint(data: bytes) -> str:
    """A short fingerprint of the file: the first 8 hex digits of its SHA-256, line endings counted as Unix ones
    (a file copied through a Windows editor is the same file)."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:8]


def read_bytes(path) -> bytes:
    """A strategy file's bytes: a small regular file, never followed through a link."""
    p = Path(path)
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise StrategyFileError([f"There is no strategy file {p.name} in {p.parent}."]) from None
    except OSError as exc:
        raise StrategyFileError([f"The strategy file {p.name} cannot be opened ({exc.strerror or exc})."]) from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise StrategyFileError([f"{p.name} is not a regular file."])
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise StrategyFileError([f"The file is larger than {MAX_FILE_BYTES // 1024} KB; a strategy file is a few "
                                 "lines of settings."])
    return data


def load(path, perps=None) -> Spec:
    """Read and check one strategy file; its name inside must match the file's name."""
    p = Path(path)
    return check_bytes(read_bytes(p), file_name=p.stem if p.suffix == ".toml" else None, perps=perps)


def load_for(cfg, perps=None) -> Spec | None:
    """The configured strategy file, checked (None for a built-in). Raises StrategyFileError."""
    if not is_file_setting(cfg.strategy):
        return None
    return load(path_for(cfg.strategies_dir, setting_name(cfg.strategy)), perps=perps)


def _q(value) -> str:
    """A value from the file, quoted for a sentence: printable characters only, and short."""
    s = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return '"' + (s if len(s) <= 40 else s[:37] + "...") + '"'


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return abs(v) < 1e300 if isinstance(v, int) else math.isfinite(v)  # a huge integer is no setting either


def _list_words(items, conj="and"):
    items = [str(x) for x in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {conj} " + items[-1]


def check_bytes(data: bytes, file_name: str | None = None, perps=None) -> Spec:
    """Check a strategy file's bytes against the menu. Returns the Spec, or raises StrategyFileError with one plain
    sentence per problem. ``perps``: the perpetuals the exchange lists now (None leaves that check out, as the daily
    run does: the coins were checked when the file was taken)."""
    if len(data) > MAX_FILE_BYTES:
        raise StrategyFileError([f"The file is larger than {MAX_FILE_BYTES // 1024} KB; a strategy file is a few "
                                 "lines of settings."])
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise StrategyFileError(["The file is not plain text (UTF-8)."]) from None
    try:
        doc = tomllib.loads(text)
    except ValueError as exc:  # TOMLDecodeError, or a number too long for Python to read
        raise StrategyFileError([f"The file is not valid TOML: {exc}."]) from None
    problems = []
    say = problems.append

    for key in doc:
        if key not in TOP_KEYS:
            say(f"Unknown setting {_q(key)}: the settings are {_list_words(TOP_KEYS)}.")

    name = doc.get("name")
    if name is None:
        say('name is missing: give the strategy a name, like name = "my-trend".')
    elif not isinstance(name, str) or not NAME_RE.match(name):
        say('name must be 1 to 40 lowercase letters, digits or dashes, starting with a letter, like "my-trend".')
    elif file_name is not None and name != file_name:
        say(f"The name inside the file ({name}) differs from the file's name ({file_name}.toml); make them the same.")

    description = doc.get("description", "")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION or any(
            not (ch.isprintable() or ch in "\n\t") for ch in description):
        say(f"description must be plain text of at most {MAX_DESCRIPTION} characters.")
        description = ""

    coins = _check_coins(doc.get("coins"), perps, say)

    rebalance = doc.get("rebalance")
    if rebalance is None:
        say('rebalance is missing: say how often it trades, "daily", "weekly" or "monthly".')
    elif rebalance not in REBALANCE:
        say(f'rebalance must be "daily", "weekly" or "monthly", not {_q(rebalance)}.')

    direction = doc.get("direction")
    if direction is None:
        say('direction is missing: "long-short" (bets on rises and falls), "long-only" or "short-only".')
    elif direction not in DIRECTIONS:
        say(f'direction must be "long-short", "long-only" or "short-only", not {_q(direction)}.')

    band = doc.get("no_trade_band", DEFAULT_BAND)
    if not _is_num(band) or not 0 <= band <= MAX_BAND:
        say("no_trade_band must be a number from 0 to 0.5 (a share of the money: 0.02 is 2%).")
        band = DEFAULT_BAND

    raw_signal = doc.get("signal")
    signal = None
    if raw_signal is None:
        say('signal is missing: add a [signal] table with its kind, like kind = "momentum".')
    elif not isinstance(raw_signal, dict):
        say('signal must be a table: a [signal] line, then kind = "momentum" and its settings.')
    else:
        signal = _check_signal(raw_signal, "signal", coins, say, top=True)

    kind = signal.kind if signal else (raw_signal.get("kind") if isinstance(raw_signal, dict) else None)
    sizing = _check_sizing(doc.get("sizing"), kind, say)

    if signal is not None and signal.kind == "fixed" and sizing is not None:
        gross, cap = sizing["gross"], sizing["cap"]
        total = sum(abs(w) for _c, w in signal.weights)
        if total > gross + 1e-12:
            say(f"The fixed weights add up to {total:g} of the money, more than sizing.gross_exposure ({gross:g}).")
        for c, w in signal.weights:
            if abs(w) > cap + 1e-12:
                say(f"signal.weights.{c} ({w:g}) is more than sizing.max_per_coin ({cap:g}).")
            if direction == "long-only" and w < 0:
                say(f'signal.weights.{c} is negative, a bet on falling, but direction is "long-only".')
            if direction == "short-only" and w > 0:
                say(f'signal.weights.{c} is positive, a bet on rising, but direction is "short-only".')

    if problems:
        raise StrategyFileError(problems)
    return Spec(name, description.strip(), tuple(coins), rebalance, direction, float(band), signal,
                sizing["method"], sizing["days"], sizing["target"], sizing["cov"], float(sizing["gross"]),
                float(sizing["cap"]), fingerprint(data))


def _check_coins(coins, perps, say):
    if not isinstance(coins, list) or not 1 <= len(coins) <= MAX_COINS:
        say(f'coins must be a list of 1 to {MAX_COINS} Hyperliquid perpetuals, like coins = ["BTC", "ETH"].')
        return []
    out = []
    listed = None if perps is None else set(perps)
    by_upper = {} if listed is None else {p.upper(): p for p in listed}
    for c in coins:
        if not isinstance(c, str) or not COIN_RE.match(c):
            say(f"{_q(c)} in coins is not a coin name (letters and digits only, as Hyperliquid writes it).")
            continue
        if c in out:
            say(f"{c} is listed twice in coins.")
            continue
        if listed is not None and c not in listed:
            hint = by_upper.get(c.upper())
            say(f"{c} is not a perpetual on Hyperliquid." + (f" Did you mean {hint}?" if hint else ""))
            continue
        out.append(c)
    return out


def _int_in(v, lo, hi):
    return _is_int(v) and lo <= v <= hi


def _check_signal(raw, where, coins, say, top=False):
    kind = raw.get("kind")
    allowed_kinds = KINDS if top else tuple(k for k in KINDS if k != "average")
    if kind is None:
        say(f"{where}.kind is missing: one of {_list_words(allowed_kinds, 'or')}.")
        return None
    if kind == "average" and not top:
        say(f'{where}.kind cannot be "average" inside an average.')
        return None
    if kind not in KINDS:
        say(f"{where}.kind must be one of {_list_words(allowed_kinds, 'or')}, not {_q(kind)}.")
        return None
    keys = SIGNAL_KEYS[kind]
    extra = ("kind",) if top else ("kind", "weight")
    for k in raw:
        if k not in keys + extra:
            say(f"Unknown setting {_q(where + '.' + str(k))} for {kind}: it takes {_list_words(keys)}.")
    for k in keys:
        if k not in raw and k not in SIGNAL_OPTIONAL.get(kind, ()):
            say(f"{where}.{k} is missing: {kind} needs {_list_words(keys)}.")
    ok = True

    def bad(sentence):
        nonlocal ok
        ok = False
        say(sentence)

    s = {"kind": kind}
    if kind == "momentum" and "lookbacks" in raw:
        lb = raw["lookbacks"]
        if (not isinstance(lb, list) or not 1 <= len(lb) <= 5 or not all(_int_in(x, 2, MAX_DAYS) for x in lb)
                or len(set(lb)) != len(lb)):
            bad(f"{where}.lookbacks must be a list of 1 to 5 different whole numbers of days from 2 to {MAX_DAYS}, "
                "like [20, 60, 120].")
        else:
            s["lookbacks"] = tuple(lb)
    elif kind == "moving-average-cross":
        fast, slow = raw.get("fast"), raw.get("slow")
        if "fast" in raw and not _int_in(fast, 2, MAX_DAYS):
            bad(f"{where}.fast must be a whole number of days from 2 to {MAX_DAYS}.")
        if "slow" in raw and not _int_in(slow, 2, MAX_DAYS):
            bad(f"{where}.slow must be a whole number of days from 2 to {MAX_DAYS}.")
        elif _int_in(fast, 2, MAX_DAYS) and _int_in(slow, 2, MAX_DAYS) and slow <= fast:
            bad(f"{where}.slow ({slow}) must be longer than {where}.fast ({fast}).")
        s.update(fast=fast, slow=slow)
    elif kind == "mean-reversion":
        days, lo, hi = raw.get("days"), raw.get("buy_below"), raw.get("sell_above")
        if "days" in raw and not _int_in(days, 2, 100):
            bad(f"{where}.days must be a whole number of days from 2 to 100.")
        if "buy_below" in raw and not (_is_num(lo) and 0 < lo < 100):
            bad(f"{where}.buy_below must be a number between 0 and 100.")
        if "sell_above" in raw and not (_is_num(hi) and 0 < hi < 100):
            bad(f"{where}.sell_above must be a number between 0 and 100.")
        elif _is_num(lo) and 0 < lo < 100 and _is_num(hi) and hi <= lo:
            bad(f"{where}.sell_above ({hi:g}) must be above {where}.buy_below ({lo:g}).")
        s.update(days=days, buy_below=float(lo) if _is_num(lo) else 0.0, sell_above=float(hi) if _is_num(hi) else 0.0)
    elif kind == "funding":
        days, rate = raw.get("days"), raw.get("min_annual_rate", 0.0)
        if "days" in raw and not _int_in(days, 1, MAX_FUNDING_DAYS):
            bad(f"{where}.days must be a whole number of days from 1 to {MAX_FUNDING_DAYS}.")
        if not (_is_num(rate) and 0 <= rate <= 5):
            bad(f"{where}.min_annual_rate must be a number from 0 to 5 (a yearly rate: 0.1 is 10% a year).")
        s.update(days=days, min_annual_rate=float(rate) if _is_num(rate) else 0.0)
    elif kind == "fixed" and "weights" in raw:
        w = raw["weights"]
        if not isinstance(w, dict):
            bad(f"{where}.weights must be a table with one number per coin, like weights = {{ BTC = 0.6, ETH = 0.4 }}.")
        else:
            for c in w:
                if c not in coins:
                    bad(f"{where}.weights names {_q(c)}, which is not in coins.")
            pairs = []
            for c in coins:
                if c not in w:
                    bad(f"{where}.weights has no weight for {c}.")
                elif not (_is_num(w[c]) and -1 <= w[c] <= 1):
                    bad(f"{where}.weights.{c} must be a number from -1 to 1 (negative is a bet on falling).")
                else:
                    pairs.append((c, float(w[c])))
            s["weights"] = tuple(pairs)
    elif kind == "average" and "parts" in raw:
        parts = raw["parts"]
        if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_PARTS or not all(isinstance(p, dict) for p in parts):
            bad(f"{where}.parts must be a list of 2 to {MAX_PARTS} signals, each written as [[signal.parts]] with its "
                "kind, its weight and its settings.")
        else:
            out = []
            for n, p in enumerate(parts, 1):
                pw = f"{where}.parts[{n}]"
                weight = p.get("weight")
                if weight is None:
                    bad(f"{pw}.weight is missing: how much this signal counts, a number above 0 and at most 10.")
                elif not (_is_num(weight) and 0 < weight <= 10):
                    bad(f"{pw}.weight must be a number above 0 and at most 10.")
                sub = _check_signal(p, pw, coins, say)
                if sub is None:
                    ok = False
                elif _is_num(weight) and 0 < weight <= 10:
                    out.append((float(weight), sub))
            s["parts"] = tuple(out)
    if not ok or any(k not in raw and k not in SIGNAL_OPTIONAL.get(kind, ()) for k in keys):
        return None
    return Signal(**s)


def _check_sizing(raw, kind, say):
    fixed = kind == "fixed"
    if raw is None:
        if fixed:
            raw = {}
        else:
            say('sizing is missing: add a [sizing] table with method = "equal" or "volatility".')
            return None
    if not isinstance(raw, dict):
        say('sizing must be a table: a [sizing] line, then method = "equal" or "volatility" and its settings.')
        return None
    method = raw.get("method")
    ok = True

    def bad(sentence):
        nonlocal ok
        ok = False
        say(sentence)

    if fixed:
        if "method" in raw:
            bad("A fixed strategy sets its own sizes in signal.weights, so sizing.method does not apply; leave it out.")
        method = "fixed"
    elif method is None:
        bad('sizing.method is missing: "equal" or "volatility".')
        return None
    elif method not in METHODS:
        bad(f'sizing.method must be "equal" or "volatility", not {_q(method)}.')
        return None
    keys = SIZING_KEYS[method]
    for k in raw:
        if k not in keys and not (fixed and k == "method"):
            say(f"Unknown setting {_q('sizing.' + str(k))} for {'a fixed signal' if fixed else method + ' sizing'}: it "
                f"takes {_list_words(keys)}.")
            ok = False
    days = raw.get("days")
    if method == "volatility":
        if days is None:
            bad("sizing.days is missing: over how many days the price swings are measured, like days = 60.")
        elif not _int_in(days, 10, MAX_DAYS):
            bad(f"sizing.days must be a whole number of days from 10 to {MAX_DAYS}.")
    target = raw.get("target_volatility")
    if target is not None and not (_is_num(target) and 0 < target <= 1):
        bad("sizing.target_volatility must be a number above 0 and at most 1 (a yearly swing: 0.2 is 20%).")
    cov = raw.get("covariance_days")
    if cov is not None:
        if target is None:
            bad("sizing.covariance_days only applies together with sizing.target_volatility.")
        elif not _int_in(cov, 10, MAX_DAYS):
            bad(f"sizing.covariance_days must be a whole number of days from 10 to {MAX_DAYS}.")
    gross = raw.get("gross_exposure", DEFAULT_GROSS)
    if not (_is_num(gross) and 0 < gross <= MAX_GROSS):
        bad(f"sizing.gross_exposure must be above 0 and at most {MAX_GROSS:g} ({MAX_GROSS * 100:g}% of the money, the "
            f"most the bot's limits allow), not {_q(gross)}.")
    cap = raw.get("max_per_coin", DEFAULT_PER_COIN)
    if not (_is_num(cap) and 0 < cap <= MAX_PER_COIN):
        bad(f"sizing.max_per_coin must be above 0 and at most {MAX_PER_COIN:g} (all the money in one coin), "
            f"not {_q(cap)}.")
    if not ok:
        return None
    return {"method": method, "days": int(days) if method == "volatility" else 0,
            "target": float(target) if target is not None else None,
            "cov": int(cov) if cov is not None else DEFAULT_COVARIANCE_DAYS,
            "gross": float(gross), "cap": float(cap)}


# ---- plain words


def _pct(x):
    return f"{x * 100:g}%"


def _days(lbs):
    return _list_words(lbs) + (" day" if list(lbs) == [1] else " days")


WHEN = {"daily": "Every day", "weekly": "Every Monday", "monthly": "On the first day of every month"}
WHEN_SHORT = {"daily": "every day", "weekly": "every Monday", "monthly": "on the first day of every month"}


def _signal_sentence(s, coins_words, nested=False):
    if s.kind == "momentum":
        if len(s.lookbacks) == 1:
            return (f"it looks at whether {coins_words} rose or fell over the last {_days(s.lookbacks)}, and follows "
                    "that move")
        return (f"it looks at how {coins_words} moved over the last {_days(s.lookbacks)}; for each coin a rise over "
                "a period counts as a vote for rising and a fall as a vote for falling, and the votes are averaged")
    if s.kind == "moving-average-cross":
        return (f"it compares the average price of {coins_words} over the last {s.fast} days with the average over the "
                f"last {s.slow} days: the shorter average above the longer one counts as rising, below it as falling")
    if s.kind == "mean-reversion":
        return (f"it measures how much of the daily moves of {coins_words} over the last {s.days} days were rises, on "
                f"a scale from 0 to 100; below {s.buy_below:g} it bets on a rise back, above {s.sell_above:g} on a "
                "fall back, and in between it holds nothing in that coin")
    if s.kind == "funding":
        floor = (f", but only when that funding came to more than {_pct(s.min_annual_rate)} a year"
                 if s.min_annual_rate > 0 else "")
        return (f"it looks at the funding of {coins_words} over the last {s.days} day{'s' if s.days != 1 else ''}: "
                "where bets on rising paid bets on falling it bets on falling, and the other way round, so it is on "
                f"the side that receives the funding{floor}")
    if s.kind == "fixed" and nested:
        lean = [f"{c} {w:+g}" for c, w in s.weights if w]
        return "leans the same way every day, " + (_list_words(lean) if lean else "nowhere") + " on a scale from -1 to 1"
    if s.kind == "fixed":
        parts = []
        for c, w in s.weights:
            if w == 0:
                parts.append(f"nothing in {c}")
            else:
                parts.append(f"{_pct(abs(w))} of the money as a bet on {c} {'rising' if w > 0 else 'falling'}")
        return "it sets back " + _list_words(parts)
    if s.kind == "average":
        total = sum(w for w, _p in s.parts)
        bits = [f"({n}) with {_pct(w / total)} of the say, {_signal_sentence(p, 'each coin', nested=True).removeprefix('it ')}"
                for n, (w, p) in enumerate(s.parts, 1)]
        return f"it combines {len(s.parts)} signals for {coins_words}: " + "; ".join(bits)
    raise ValueError(s.kind)


def explain(spec: Spec) -> str:
    """What the bot understood, in plain words, a few sentences."""
    coins = _list_words(spec.coins)
    out = [f"{WHEN[spec.rebalance]}, {_signal_sentence(spec.signal, coins)}."]
    if spec.signal.kind != "fixed":
        out.append({"long-short": "It bets on rises and on falls.",
                    "long-only": "It only bets on rises: where the signal says falling, it holds nothing.",
                    "short-only": "It only bets on falls: where the signal says rising, it holds nothing."}
                   [spec.direction])
    limits = (f"at most {_pct(spec.max_per_coin)} of the money in one coin and {_pct(spec.gross_exposure)} in all")
    if spec.method == "fixed":
        out.append(f"It never holds more than {_pct(spec.gross_exposure)} of the money in all.")
    elif spec.method == "equal":
        out.append(f"The money is shared equally among the coins it can trade that day, a weaker signal getting a "
                   f"smaller share, with {limits}.")
    elif spec.target_volatility is not None:
        out.append(f"It sizes each coin by how much its price swung over the last {spec.volatility_days} days (a "
                   "calmer coin gets more), scaled so the whole book's price swings come to about "
                   f"{_pct(spec.target_volatility)} a year by the last {spec.covariance_days} days' standards, "
                   f"with {limits}.")
    else:
        out.append(f"Each coin's share is set by how much its price swung over the last {spec.volatility_days} "
                   f"days: a coin that swings half as much gets twice the share, with {limits}.")
    if spec.no_trade_band > 0:
        out.append(f"It leaves a coin alone while its position is within {_pct(spec.no_trade_band)} of the money of "
                   "its target, unless the bet turns around or ends, or the position has grown past its limits.")
    out.append(f"A coin is traded once it has {MIN_LISTING_DAYS} days of daily prices on Hyperliquid. These are bets "
               "on the price through perpetual contracts; it never buys the coins themselves.")
    return " ".join(out)


def summary(spec: Spec) -> str:
    """One line: what it does and when."""
    s = spec.signal
    what = {
        "momentum": lambda: f"follows the moves over {_days(s.lookbacks)}",
        "moving-average-cross": lambda: f"compares the {s.fast} and {s.slow} day average prices",
        "mean-reversion": lambda: f"bets against stretched moves over {s.days} days",
        "funding": lambda: f"takes the side that receives funding over {s.days} day{'s' if s.days != 1 else ''}",
        "fixed": lambda: "holds fixed shares",
        "average": lambda: f"combines {len(s.parts)} signals",
    }[s.kind]()
    return f"{what} on {_list_words(spec.coins)}, {WHEN_SHORT[spec.rebalance]}"


def words(cfg) -> str:
    """The configured strategy in plain words, for status, the page and the weekly report."""
    if not is_file_setting(cfg.strategy):
        return STRATEGY_WORDS[cfg.strategy]
    name = setting_name(cfg.strategy)
    try:
        spec = load_for(cfg)
    except StrategyFileError as exc:
        return f'your own strategy "{name}" (its file cannot be used: {exc.problems[0]})'
    return f'your own strategy "{name}": {summary(spec)}'


# ---- the engine


def rebalance_due(spec: Spec, books: Books, day: date) -> bool:
    """Whether the decision for ``day`` trades: on its rebalance days, on the first day, and on the day the
    strategy (or its file) changed."""
    if books.strategy != spec.book_id or books.start_day == day:
        return True
    if spec.rebalance == "weekly":
        return day.weekday() == 0
    if spec.rebalance == "monthly":
        return day.day == 1
    return True


def fresh_books(equity, start_day, setting, spec: Spec | None = None) -> Books:
    """Fresh virtual books for the configured strategy: a file strategy owns the whole equity in the trend book."""
    if spec is None:
        return Books.fresh(equity, start_day, setting)
    return Books(TrendBookState(equity), CarryBookState(0.0), start_day, spec.book_id)


def _closes_in_run(history, i, d, n):
    """The last ``n`` closes of coin ``i`` through day ``d``, or None unless all lie in the current run of valid
    days (the same rule as the built-in trend signal)."""
    start = history.perp_segment[d, i]
    if not np.isfinite(start) or d - n + 1 < start:
        return None
    return history.perp_close[d - n + 1 : d + 1, i]


def score(history, s: Signal, i, d):
    """The signal for coin ``i`` from the history through day ``d``: a number from -1 (falling) to 1 (rising), or
    None when there is not enough history."""
    if s.kind == "momentum":
        return trend_signal(history.perp_close[:, i], history.perp_segment[:, i], d, s.lookbacks)
    if not np.isfinite(history.perp_segment[d, i]):
        return None
    if s.kind == "moving-average-cross":
        c = _closes_in_run(history, i, d, s.slow)
        if c is None:
            return None
        return float(np.sign(float(np.mean(c[-s.fast:])) - float(np.mean(c))))
    if s.kind == "mean-reversion":
        c = _closes_in_run(history, i, d, s.days + 1)
        if c is None:
            return None
        diff = np.diff(c)
        up, down = float(np.mean(np.maximum(diff, 0.0))), float(np.mean(np.maximum(-diff, 0.0)))
        strength = 50.0 if up + down == 0 else 100.0 * up / (up + down)
        return 1.0 if strength < s.buy_below else (-1.0 if strength > s.sell_above else 0.0)
    if s.kind == "funding":
        if d - s.days + 1 < 0:
            return None
        ann = trailing_annualized(history.rates, d, i, s.days)
        return 0.0 if abs(ann) <= s.min_annual_rate else -float(np.sign(ann))
    if s.kind == "fixed":
        return dict(s.weights).get(history.coins[i], 0.0)
    if s.kind == "average":
        total, weight = 0.0, 0.0
        for w, part in s.parts:
            x = score(history, part, i, d)
            if x is None:
                return None
            total += w * x
            weight += w
        return total / weight
    raise ValueError(s.kind)


def _limit(targets, cap, gross):
    out = {i: max(-cap, min(cap, w)) for i, w in targets.items()}
    total = sum(abs(w) for w in out.values())
    if total > gross * (1 + 1e-12):
        out = {i: w * (gross / total) for i, w in out.items()}
    return out


def target_weights(history, spec: Spec, cands, d):
    """{coin index: weight}, signed shares of the book's equity, for the coins with a signal today."""
    gross = min(spec.gross_exposure, MAX_GROSS)
    cap = min(spec.max_per_coin, MAX_PER_COIN)
    ids, sig = [], []
    for i in cands:
        s = score(history, spec.signal, i, d)
        if s is None or not math.isfinite(s):  # no number, never a position
            continue
        if spec.direction == "long-only":
            s = max(s, 0.0)
        elif spec.direction == "short-only":
            s = min(s, 0.0)
        if s == 0:
            continue
        if spec.method == "volatility" and not history.perp_segment[d, i] <= d - spec.volatility_days:
            continue
        ids.append(i)
        sig.append(s)
    if not ids:
        return {}
    if spec.method == "fixed":
        return _limit(dict(zip(ids, sig, strict=True)), cap, gross)
    if spec.method == "equal":
        pool = [i for i in cands if np.isfinite(history.perp_segment[d, i])]
        return _limit({i: s * gross / len(pool) for i, s in zip(ids, sig, strict=True)}, cap, gross)
    days = spec.volatility_days
    if spec.target_volatility is not None:
        # The built-in trend book's sizing, unchanged: signal over volatility, scaled to the target with the
        # shrunk covariance, then the cap per coin and the gross cap.
        rv = history.log_ret[d - days + 1 : d + 1][:, ids]
        rc = history.log_ret[max(0, d - spec.covariance_days + 1) : d + 1][:, ids]
        caps = np.array([cap] * len(ids))
        w = vol_scaled_weights(sig, rv, rc, spec.target_volatility, caps, gross)
        if w is None:
            return {}
        return {i: float(x) for i, x in zip(ids, w, strict=True)}
    # Inverse volatility: each coin's share of the gross exposure is set by how calm its price was, among the coins
    # it can trade today.
    pool = [i for i in cands if history.perp_segment[d, i] <= d - days]
    inv = {}
    for i in pool:
        sigma = float(np.std(history.log_ret[d - days + 1 : d + 1, i], ddof=1)) * math.sqrt(ANNUAL_DAYS)
        if math.isfinite(sigma) and sigma > 0:
            inv[i] = 1.0 / sigma
    total = sum(inv.values())
    if total <= 0:
        return {}
    return _limit({i: s * gross * inv[i] / total for i, s in zip(ids, sig, strict=True) if i in inv}, cap, gross)


def _decide_own(history, book, day, spec, due):
    """The file strategy's targets on its book, with the built-in trend book's band and order rules."""
    d = len(history.dates) - 1
    equity = book.equity
    idx = history.index
    cands = [idx[c] for c in spec.coins if c in idx and _age_ok(history, day, idx[c], MIN_LISTING_DAYS)]
    in_universe = set(cands)
    held = sorted(idx[c] for c, q in book.q.items() if q and c in idx)
    orders, weights = {}, {}
    if equity <= 0:
        orders = {i: 0.0 for i in held}
    elif due:
        targets = target_weights(history, spec, cands, d)
        weights = {history.coins[i]: w for i, w in targets.items()}
        cap, gross = min(spec.max_per_coin, MAX_PER_COIN), min(spec.gross_exposure, MAX_GROSS)
        kept, kept_weight = {}, 0.0
        for i in sorted(in_universe | set(held)):
            price = history.perp_close[d, i]
            q = book.q.get(history.coins[i], 0.0)
            w = targets.get(i, 0.0)
            if not np.isfinite(price):
                # No valid close yesterday (a quiet coin): left as it is, as the built-in book does, but valued at its
                # last valid close, so it still counts toward the limits and is cut back when it is beyond them.
                seen = np.flatnonzero(np.isfinite(history.perp_close[: d + 1, i]))
                if not q or not len(seen):
                    continue
                price = float(history.perp_close[seen[-1], i])
                cw = q * price / equity
                if abs(cw) > cap + LIMIT_DRIFT:
                    orders[i] = w * equity / price
                else:
                    kept[i] = (w, price)
                    kept_weight += abs(cw)
                continue
            cw = q * price / equity if q else 0.0
            if needs_trade(w, cw, i in in_universe, spec.no_trade_band) or abs(cw) > cap + LIMIT_DRIFT:
                orders[i] = w * equity / price  # the band never keeps a coin above its limit by more than the drift
            else:
                kept[i] = (w, price)
                kept_weight += abs(cw)
        if kept and kept_weight + sum(abs(targets.get(i, 0.0)) for i in orders) > gross + LIMIT_DRIFT * len(spec.coins):
            for i, (w, price) in kept.items():  # nor the whole book above its gross exposure: all to their targets
                orders[i] = w * equity / price
    return {history.coins[i]: v for i, v in orders.items()}, weights


def decide(history, books: Books, spec: Spec, equity: float | None = None, day: date | None = None) -> Decision:
    """Target positions for day ``history.last_day + 1`` from a strategy file: the same inputs and the same Decision
    as ``strategy.decide``. The file strategy runs in the trend book; anything the carry book still holds (after a
    switch from a built-in) is closed, and the whole equity moves to the file strategy, as a switch between built-ins
    does."""
    expected = history.last_day.toordinal() + 1
    if day is None:
        day = date.fromordinal(expected)
    elif day.toordinal() != expected:
        raise ValueError(f"a decision for {day} needs history through {date.fromordinal(day.toordinal() - 1)}, "
                         f"got {history.last_day}")
    due = rebalance_due(spec, books, day)
    b = books.copy()
    if b.strategy != spec.book_id:
        b.trend.equity, b.carry.equity = b.equity, 0.0
        b.strategy = spec.book_id
    trend_orders, weights = _decide_own(history, b.trend, day, spec, due)
    carry_orders, _active, ann7, elig = _decide_carry(history, b.carry, day, CarryConfig(), (), MIN_LISTING_DAYS)
    after = b.copy()
    for c, q in trend_orders.items():
        if q:
            after.trend.q[c] = q
        else:
            after.trend.q.pop(c, None)
    for c in carry_orders:
        after.carry.perp_q.pop(c, None)
        after.carry.spot_q.pop(c, None)
        after.carry.balance_price.pop(c, None)
    after.carry.active, after.carry.last_set = [], []
    total = books.equity
    scale = 1.0 if equity is None else (equity / total if total > 0 else 0.0)
    return Decision(day, False, b.trend.equity, b.carry.equity, trend_orders, carry_orders, [], weights, ann7, elig,
                    scale, after, [])


__all__ = [
    "PREFIX", "Spec", "Signal", "StrategyFileError", "check_bytes", "check_setting", "decide", "explain", "fingerprint",
    "fresh_books", "is_file_setting", "load", "load_for", "path_for", "rebalance_due", "score", "setting_name",
    "summary", "target_weights", "words",
]
