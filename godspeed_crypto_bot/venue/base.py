"""The venue interface. The strategy never sees it; the executor and the guard only see this.

Hyperliquid (venue/hyperliquid.py) is the first implementation, the simulated account
(venue/simulated.py) the second. A later exchange adds a third file here and nothing else changes:
strategy.py works on coins, days and quantities only.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Instrument:
    coin: str  # the strategy's coin: "BTC"
    market: str  # "perp" or "spot"
    name: str  # the venue's name for it: "BTC", "@142"
    sz_decimals: int
    px_decimals: int  # most decimals a price may carry
    token: str | None = None  # spot base token: "UBTC"
    max_leverage: int | None = None

    @property
    def key(self):
        return f"{self.market}:{self.coin}"

    def round_size(self, size):
        """Round a size down to the instrument's step (never over-trade)."""
        step = 10 ** -self.sz_decimals
        return round(math.floor(abs(size) / step + 1e-9) * step, self.sz_decimals) * (1 if size >= 0 else -1)

    def round_price(self, px):
        """Five significant figures and at most px_decimals decimals; integers always allowed."""
        if px <= 0:
            raise ValueError("price must be positive")
        if px >= 10_000:
            return float(round(px))
        return round(float(f"{px:.5g}"), self.px_decimals)


def coins_of(instruments) -> tuple:
    """Every coin an instruments map covers (perp or spot), in its order."""
    return tuple(dict.fromkeys(coin for coin, _market in instruments))


@dataclass
class Candle:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int
    closed: bool


@dataclass
class Position:
    coin: str
    size: float  # signed
    entry_px: float | None = None
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    liquidation_px: float | None = None
    notional: float = 0.0


TWO_BALANCE_MODES = ("standard", "disabled", "simulated")  # separate spot and perp USDC: transfers between them
UNIFIED_MODES = ("unifiedAccount", "simulated-unified")  # one USDC balance for spot and the USDC cross perps


@dataclass
class AccountState:
    """What the account holds. Perp values in USDC; spot as token -> (total, hold).

    Separate balances ('disabled'): perp_value is the perp account's value. Unified ('unifiedAccount'): there is
    one USDC balance (Hyperliquid reports it in the spot state, without unrealized PnL), which pays for spot buys
    and collateralizes the cross perps; perp_value is that balance plus the perps' unrealized PnL, so leverage and
    margin are measured against the same money in both modes.
    """

    perp_value: float  # USDC collateral of the cross perps, including unrealized PnL
    perp_withdrawable: float
    maint_margin_used: float
    total_margin_used: float
    positions: dict = field(default_factory=dict)  # coin -> Position
    spot: dict = field(default_factory=dict)  # token -> (total, hold)
    mode: str = "standard"

    @property
    def unified(self):
        return self.mode in UNIFIED_MODES

    @property
    def two_balances(self):
        return self.mode in TWO_BALANCE_MODES

    @property
    def usdc_equity(self):
        """All the account's USDC including unrealized PnL (in Unified the spot USDC IS the perp collateral)."""
        return self.perp_value if self.unified else self.perp_value + self.spot_usdc

    @property
    def perp_notional(self):
        return sum(abs(p.notional) for p in self.positions.values())

    @property
    def spot_usdc(self):
        return self.spot.get("USDC", (0.0, 0.0))[0]

    @property
    def spot_usdc_free(self):
        total, hold = self.spot.get("USDC", (0.0, 0.0))
        return max(0.0, total - hold)

    def perp_size(self, coin):
        p = self.positions.get(coin)
        return p.size if p else 0.0

    def spot_size(self, token):
        return self.spot.get(token, (0.0, 0.0))[0] if token else 0.0

    def account_value(self, spot_prices):
        """The account's USDC (perp value plus spot USDC, or the one unified balance) plus the strategy's spot
        tokens at the given prices.

        Only tokens in ``spot_prices`` (the strategy's: UBTC, UETH, USOL) count; anything else sitting in
        the account is not the strategy's money and is ignored.
        """
        value = self.usdc_equity
        for token, (total, _hold) in self.spot.items():
            if token == "USDC" or not total or token not in spot_prices:
                continue
            value += total * spot_prices[token]
        return value


@dataclass
class OrderResult:
    status: str  # "filled", "partial", "unfilled", "rejected", "error"
    requested: float
    filled: float = 0.0
    avg_px: float | None = None
    fee: float | None = None
    oid: int | None = None
    error: str | None = None
    raw: object = None

    @property
    def ok(self):
        return self.status in ("filled", "partial")


class MarketData(ABC):
    """Public market data. No key, no account."""

    network: str = "mainnet"

    @abstractmethod
    def instruments(self) -> dict:
        """(coin, market) -> Instrument for every coin the strategy may trade."""

    @abstractmethod
    def daily_candles(self, inst: Instrument, start: date, end: date) -> list:
        """Daily UTC candles for start..end inclusive; the running day, if included, has closed=False."""

    @abstractmethod
    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list:
        """[(time_ms, rate)] perp funding settlements; positive means longs pay."""

    @abstractmethod
    def mids(self) -> dict:
        """Instrument name -> mid price."""

    @abstractmethod
    def l2_book(self, inst: Instrument) -> tuple:
        """(bids, asks): lists of (price, size), best first."""

    @abstractmethod
    def perp_marks(self) -> dict:
        """Perp coin -> mark price."""

    def first_trade_day(self, inst: Instrument) -> date | None:
        """First day the instrument traded, if the venue can say."""
        return None


class Venue(ABC):
    """An account on a venue. ``signs`` is True only where orders are signed and sent to an exchange."""

    signs = False

    def __init__(self, market: MarketData):
        self.market = market

    @abstractmethod
    def account(self) -> AccountState: ...

    @abstractmethod
    def place_ioc(self, inst: Instrument, is_buy: bool, size: float, limit_px: float, cloid: str,
                  reduce_only: bool = False) -> OrderResult: ...

    @abstractmethod
    def transfer_usdc(self, amount: float, to_perp: bool) -> bool: ...

    @abstractmethod
    def set_leverage(self, coin: str, leverage: int) -> None: ...

    @abstractmethod
    def funding_payments(self, start_ms: int, end_ms: int) -> list:
        """[{time, coin, usdc, szi, rate}] funding actually paid or received by this account."""

    def fills(self, start_ms: int, end_ms: int) -> list:
        return []

    def ledger_updates(self, start_ms: int, end_ms: int) -> list:
        """The venue's own records of deposits, withdrawals and transfers (raw)."""
        return []

    def agent_validity(self):
        """{address, valid_until_ms, listed} for the signing agent; None where nothing signs."""
        return None

    def sync_records(self, store, start_ms: int, end_ms: int) -> dict:
        """Copy the venue's fills, funding and ledger (with its hashes and ids) into the records."""
        return {}

    def external_flows(self, start_ms: int) -> float:
        """Deposits minus withdrawals since start_ms (USDC). Internal spot/perp moves are not flows."""
        return 0.0

    def preflight(self) -> list:
        """Problems that must be fixed before this venue may trade (empty when ready)."""
        return []
