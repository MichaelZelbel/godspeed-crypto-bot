"""The answer to the exchange's country question, kept with its date.

What is asked, and why, is the exchange's business and lives with it (terms.py). The installer asks once, and the answer is kept here with its date. A
"yes" stops the setup; going live is refused without a recorded "no". The weekly terms check (terms.py) tells
the owner when Hyperliquid's list changes.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .names import NAME
from .terms import QUESTION, REFUSAL  # the exchange's own rule and how it is put to the person (terms.py)

FILE = "eligibility.json"


def path(cfg) -> Path:
    return Path(cfg.confirm_dir) / FILE


def read(cfg):
    try:
        return json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record(cfg, restricted: bool, today: date | None = None) -> dict:
    """Keep the answer with its date (root writes it; the bot only reads it)."""
    doc = {"restricted": bool(restricted), "answered_on": (today or date.today()).isoformat(),
           "question": QUESTION}
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1) + chr(10), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
    return doc


def valid(doc) -> bool:
    """A recorded answer: an object with a true/false `restricted` and the date it was given."""
    if not isinstance(doc, dict) or not isinstance(doc.get("restricted"), bool):
        return False
    try:
        date.fromisoformat(str(doc.get("answered_on")))
    except ValueError:
        return False
    return True


def problems(cfg) -> list[str]:
    """For the live gate: a recorded answer of "no restriction" is needed."""
    doc = read(cfg)
    if doc is not None and not valid(doc):
        return [f"the country answer on file is unreadable (answer again: {NAME} eligibility --restricted no)"]
    if doc is None:
        return [f"the country question was never answered (run: {NAME} eligibility --restricted no, "
                "or the install line with --restricted no)"]
    if doc.get("restricted"):
        return [REFUSAL]
    return []
