"""Shared fakes: a deterministic market, configuration in a temporary directory, a recording notifier."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from godspeed_crypto_bot import config, outbox, records
from godspeed_crypto_bot.notify import Notifier
from godspeed_crypto_bot.venue.base import Candle, Instrument, MarketData
from godspeed_crypto_bot.venue.simulated import SimulatedVenue

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 0, 2, tzinfo=timezone.utc)

INSTRUMENTS = {
    ("BTC", "perp"): Instrument("BTC", "perp", "BTC", 5, 1, None, 40),
    ("ETH", "perp"): Instrument("ETH", "perp", "ETH", 4, 2, None, 25),
    ("SOL", "perp"): Instrument("SOL", "perp", "SOL", 2, 4, None, 20),
    ("BTC", "spot"): Instrument("BTC", "spot", "@142", 5, 3, "UBTC"),
    ("ETH", "spot"): Instrument("ETH", "spot", "@151", 4, 4, "UETH"),
    ("SOL", "spot"): Instrument("SOL", "spot", "@156", 3, 5, "USOL"),
}
BASE = {"BTC": 80_000.0, "ETH": 2_500.0, "SOL": 100.0}


def ms(dt):
    return int(dt.timestamp() * 1000)


class FakeMarket(MarketData):
    """Prices drift up 0.3% a day for 400 days; spot equals perp; deep books; hourly funding per coin."""

    network = "mainnet"

    def __init__(self, today=TODAY, funding_hourly=None, drift=0.003, depth=1e9):
        self.today = today
        self.drift = drift
        self.funding_hourly = funding_hourly or {"BTC": 0.000004, "ETH": 0.000004, "SOL": 0.000012}
        self.mid = {}
        for coin in BASE:
            p = self.close_on(coin, today - timedelta(days=1))
            self.mid[coin] = p
            self.mid[INSTRUMENTS[(coin, "spot")].name] = p
        self.marks = {c: self.mid[c] for c in BASE}
        self.depth = depth
        self.book_override = {}

    def close_on(self, coin, d):
        k = (d - (self.today - timedelta(days=400))).days
        return BASE[coin] * math.exp(self.drift * k + 0.02 * math.sin(k))

    def instruments(self):
        return dict(INSTRUMENTS)

    def daily_candles(self, inst, start, end):
        out = []
        d = max(start, self.today - timedelta(days=400))
        while d <= end:
            c = self.close_on(inst.coin, d)
            o = self.close_on(inst.coin, d - timedelta(days=1))
            out.append(Candle(d, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1000.0, 500, d < self.today))
            d += timedelta(days=1)
        return out

    def funding_history(self, coin, start_ms, end_ms):
        out = []
        t = (start_ms // 3_600_000 + 1) * 3_600_000
        while t <= end_ms:
            out.append((t + 37, self.funding_hourly[coin]))
            t += 3_600_000
        return out

    def mids(self):
        return dict(self.mid)

    def perp_marks(self):
        return dict(self.marks)

    def l2_book(self, inst):
        if inst.name in self.book_override:
            return self.book_override[inst.name]
        m = self.mid[inst.name]
        return ([(m * (1 - 0.0001 * k), self.depth) for k in range(1, 6)],
                [(m * (1 + 0.0001 * k), self.depth) for k in range(1, 6)])


@pytest.fixture
def market():
    return FakeMarket()


@pytest.fixture
def cfg(tmp_path):
    return config.Config(state_dir=str(tmp_path / "state"), confirm_dir=str(tmp_path),
                         credstore_dir=str(tmp_path / "credstore"), credentials_dir=str(tmp_path / "creds"),
                         relay_dir=str(tmp_path / "relay")).validate()


class Calls:
    """What was queued for the owner: the alert files in the outbox (the relay tells them through Godspeed)."""

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def argv(self):
        return [outbox.read_json(p) for p in outbox.waiting_alerts(self.cfg)]

    def clear(self):
        for p in outbox.waiting_alerts(self.cfg):
            p.unlink()

    def texts(self):
        return [d["text"] for d in self.argv]


@pytest.fixture
def store(cfg):
    s = records.Store(cfg.db_path, cfg.mode, cfg.network)
    yield s
    s.close()


@pytest.fixture
def calls(cfg):
    return Calls(cfg)


@pytest.fixture
def notifier(cfg, store, calls):
    return Notifier(cfg, store)


@pytest.fixture
def paper(cfg, store, market):
    return SimulatedVenue(market, store, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())
