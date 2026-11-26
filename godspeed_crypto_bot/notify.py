"""Alerts: one plain message to the owner, only when they must act or something broke and stayed broken.

An alert is queued in the outbox (outbox.py) and told through Godspeed's own channel by the relay, the way
Godspeed's other add-ons tell theirs. Every alert has a key and is sent at most once per window; the attempt
is recorded. Live trading on mainnet is real money; everything else (dry-run, paper, testnet) is marked as
practice in the message, because the owner has nothing to lose there and may have nothing to do.
"""

from __future__ import annotations

import logging
import time

from . import outbox
from .names import DISPLAY_NAME

log = logging.getLogger(__name__)
LANE_REAL = "owner"
LANE_PRACTICE = "practice"


def is_practice(cfg) -> bool:
    return not (cfg.mode == "live" and cfg.network == "mainnet")


def message(cfg, summary, status, act) -> str:
    """What the owner reads: what happened, the facts, and what to do about it."""
    tag = f" (practice, {cfg.mode} on {cfg.network})" if is_practice(cfg) else ""
    return " ".join(x for x in (f"{DISPLAY_NAME}{tag}: {summary.rstrip('. ')}.", status.strip(), act.strip()) if x)


class Notifier:
    def __init__(self, cfg, store, clock=time.time):
        self.cfg, self.store, self.clock = cfg, store, clock

    def alert(self, key, summary, status, act, window_hours=24.0, detail=None):
        """Queue the alert unless the same key was queued within the window. Returns 0 when queued, -1 when
        it could not be, None when it was already sent in the window."""
        if self.store.alerted_since(key, self.clock() - window_hours * 3600):
            return None
        code = 0 if outbox.put_alert(self.cfg, key, message(self.cfg, summary, status, act)) else -1
        self.store.add_alert(key, LANE_PRACTICE if is_practice(self.cfg) else LANE_REAL, summary, act, code,
                             {"status": status, **(detail or {})})
        return code
