"""Configuration: one TOML file, safe defaults, dry-run unless every live condition holds."""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path

from .names import CONFIG_DIR, CREDENTIAL_PREFIX, RELAY_DIR, STATE_DIR
from .strategy import DEFAULT_STRATEGY, check_strategy

MODES = ("dry-run", "paper", "live")
NETWORKS = ("mainnet", "testnet")
API_URLS = {"mainnet": "https://api.hyperliquid.xyz", "testnet": "https://api.hyperliquid-testnet.xyz"}
DEFAULT_CONFIG = Path(CONFIG_DIR) / "config.toml"
CONFIRM_NAME = "LIVE-CONFIRMED"  # the confirmation file is LIVE-CONFIRMED.<network>, one per network
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
UNIT_SPOT_TOKENS = {"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"}  # the strategy's spot legs on mainnet


@dataclass
class Limits:
    """Absolute risk limits. Zero means "derive from the capital cap" (see Config.resolved)."""

    min_order_usdc: float = 11.0  # Hyperliquid refuses orders under $10 (MinTradeNtl); $11 leaves room
    max_order_usdc: float = 0.0  # default 1.4 x cap: a full trend flip on BTC plus a carry entry
    max_day_usdc: float = 0.0  # default 4 x cap of traded notional per UTC day
    perp_slippage: float = 0.0025  # IOC limit at mid +-0.25% for the majors
    spot_slippage: float = 0.004  # IOC limit at mid +-0.4% (Unit spot books are thinner)
    unwind_slippage: float = 0.01  # unwinding a failed pair or flattening may cross wider
    retries: int = 3  # attempts per leg
    leverage: int = 3  # cross-margin ceiling set on every traded perp
    drawdown_halt: float = 0.25  # halt and flatten when equity is 25% below the starting capital
    pair_fill_ratio: float = 0.9  # a carry leg filling less than this after retries counts as failed


@dataclass
class Guard:
    trigger_leverage: float = 4.0  # perp notional / perp account value above this: act
    target_leverage: float = 2.5  # bring it back to this
    maint_multiple: float = 3.0  # also act when perp account value < 3 x maintenance margin
    spot_usdc_reserve: float = 2.0  # USDC left in the spot wallet when sweeping to perp
    shrink_step: float = 0.1  # carry pairs shrink in steps of 10%


@dataclass
class Config:
    mode: str = "dry-run"
    network: str = "mainnet"
    strategy: str = DEFAULT_STRATEGY  # "mix" (half trend, half carry), "trend" or "carry"
    master_address: str = ""
    agent_approved_on: str = ""  # YYYY-MM-DD, when the API wallet was approved (fallback for its expiry)
    agent_valid_until: str = ""  # YYYY-MM-DD, if Hyperliquid does not report validUntil
    capital_cap_usdc: float = 2000.0
    state_dir: str = STATE_DIR
    confirm_dir: str = CONFIG_DIR  # holds LIVE-CONFIRMED.<network>
    credentials_dir: str = ""  # defaults to $CREDENTIALS_DIRECTORY (systemd ImportCredential)
    credstore_dir: str = "/etc/credstore.encrypted"
    relay_dir: str = RELAY_DIR  # a second track (testnet) names a folder of its own here
    # Spot token per carry coin. Mainnet: exactly the Unit tokens. Testnet may name others or leave a coin out.
    spot_tokens: dict = field(default_factory=lambda: dict(UNIT_SPOT_TOKENS))
    # Testnet only: a carry coin whose spot token the network does not list (testnet has no UBTC) is simply not
    # carried (the strategy's has_spot), instead of preflight refusing. Refused on mainnet.
    allow_missing_spot: bool = False
    history_days: int = 400
    deadline_utc: str = "00:45"
    latest_run_hour_utc: int = 6  # a scheduled run later than this skips the day (the deadline alert has fired)
    # Godspeed (the add-on's alerts, reminder and page go through it; the relay reads these, see godspeed.py)
    godspeed_dir: str = ""  # the Godspeed Mission Control folder, set by the installer
    telegram_env: str = ""  # optional: the assistant's .env with the Telegram bot token, if not found by itself
    limits: Limits = field(default_factory=Limits)
    guard: Guard = field(default_factory=Guard)

    cap_named = False  # set by load(): the configuration itself names capital_cap_usdc (not a setting)

    @property
    def api_url(self):
        return API_URLS[self.network]

    @property
    def credential_name(self):
        return f"{CREDENTIAL_PREFIX}.{self.network}"

    @property
    def confirm_file(self):
        """LIVE-CONFIRMED.<network>: a testnet confirmation can never sit where the mainnet one is read."""
        return Path(self.confirm_dir) / f"{CONFIRM_NAME}.{self.network}"

    @property
    def state_path(self):
        return Path(self.state_dir)

    @property
    def relay_path(self):
        """Where the root relay keeps its own files (its status, the page address, the reminder record). A
        root-owned folder that is never inside the state directory, because the bot's user can write that."""
        return Path(self.relay_dir)

    @property
    def db_path(self):
        return self.state_path / "bot.sqlite"

    @property
    def halt_path(self):
        return self.state_path / "HALT"

    @property
    def reports_path(self):
        return self.state_path / "reports"

    def max_order(self):
        return self.limits.max_order_usdc or 1.4 * self.capital_cap_usdc

    def max_day(self):
        return self.limits.max_day_usdc or 4.0 * self.capital_cap_usdc

    def validate(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {self.mode!r}")
        if self.network not in NETWORKS:
            raise ValueError(f"network must be one of {NETWORKS}, not {self.network!r}")
        check_strategy(self.strategy)
        if (not isinstance(self.capital_cap_usdc, (int, float)) or isinstance(self.capital_cap_usdc, bool)
                or not math.isfinite(self.capital_cap_usdc)):
            raise ValueError("capital_cap_usdc must be a number")
        if not self.capital_cap_usdc > 0:
            raise ValueError("capital_cap_usdc must be positive")
        if self.agent_approved_on:
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", self.agent_approved_on):
                    raise ValueError
                date.fromisoformat(self.agent_approved_on)
            except (TypeError, ValueError):
                raise ValueError("agent_approved_on must be a day written YYYY-MM-DD") from None
        if self.master_address and not ADDRESS.match(self.master_address):
            raise ValueError("master_address must be a 0x-prefixed 40-hex-digit address")
        if not 1 <= self.limits.leverage <= 5:
            raise ValueError("limits.leverage must be between 1 and 5")
        if not 0 < self.limits.drawdown_halt < 1:
            raise ValueError("limits.drawdown_halt must be between 0 and 1")
        if self.limits.min_order_usdc < 10:
            raise ValueError("limits.min_order_usdc below Hyperliquid's $10 minimum")
        if not isinstance(self.spot_tokens, dict) or not all(
                k in UNIT_SPOT_TOKENS and isinstance(v, str) and v for k, v in self.spot_tokens.items()):
            raise ValueError(f"spot_tokens must map some of {sorted(UNIT_SPOT_TOKENS)} to token names")
        if self.network == "mainnet":
            if self.spot_tokens != UNIT_SPOT_TOKENS:
                raise ValueError(f"spot_tokens can differ from {UNIT_SPOT_TOKENS} only on testnet")
            if self.allow_missing_spot:
                raise ValueError("allow_missing_spot is a testnet-only setting")
        return self


def _merge(obj, data, where):
    names = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in names:
            raise ValueError(f"unknown setting {where}{key}")
        current = getattr(obj, key)
        if isinstance(current, (Limits, Guard)):
            if not isinstance(value, dict):
                raise ValueError(f"{where}{key} must be a table")
            _merge(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def config_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("GODSPEED_CRYPTO_BOT_CONFIG") or DEFAULT_CONFIG)


def load(path: str | os.PathLike | None = None, **overrides) -> Config:
    """Read the TOML file (if it exists); keyword overrides win. Unknown keys are refused."""
    cfg = Config()
    p = config_path(path)
    named = False
    if p.exists():
        with open(p, "rb") as fh:
            data = tomllib.load(fh)
        named = "capital_cap_usdc" in data
        _merge(cfg, data, "")
    given = {k: v for k, v in overrides.items() if v is not None}
    _merge(cfg, given, "")
    cfg.cap_named = named or "capital_cap_usdc" in given  # live needs a cap the person wrote, never the default
    return cfg.validate()
