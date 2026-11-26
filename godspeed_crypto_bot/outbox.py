"""The outbox: what the bot wants said to its owner, as small files the relay (godspeed.py) delivers.

The trading programs run as an unprivileged user with no way into the Godspeed server's assistant, its
Telegram bot or its deadline list. They only write files here, in the bot's own state directory, and never
wait for anyone: a failed write is logged and never stops trading or a halt. One root-owned relay unit
(`godspeed-crypto-bot relay`, started when a file appears and every five minutes) picks them up, tells the
owner through Godspeed's own channel, and keeps the key-renewal reminder in Godspeed's deadline list.

alert-<time>-<random>.json   one message: {"key", "text", "link", "created"}; deleted once a channel took it
deadline.json                the wanted key-renewal reminder: {"key", "title", "due", "done_when", "cost"};
                             rewritten only when it changes, so the relay asks Godspeed only when needed
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)
DEADLINE_FILE = "deadline.json"
MAX_FILE = 64 * 1024


_last_stamp = 0


def _next_stamp() -> int:
    """Nanoseconds, strictly increasing within this process, so messages are told in the order they were made."""
    global _last_stamp
    _last_stamp = max(time.time_ns(), _last_stamp + 1)
    return _last_stamp


def directory(cfg) -> Path:
    return cfg.state_path / "outbox"


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def put_alert(cfg, key, text, link=None) -> bool:
    """Queue one message. True when it is on disk."""
    try:
        name = f"alert-{_next_stamp():020d}-{secrets.token_hex(4)}.json"
        _write(directory(cfg) / name, {"key": key, "text": text, "link": link, "created": time.time()})
        return True
    except OSError as exc:
        log.warning("alert could not be queued (%s): %s", exc, text)
        return False


def put_deadline(cfg, doc) -> bool:
    """Ask for the key-renewal reminder. Rewrites the file only when its content changed."""
    path = directory(cfg) / DEADLINE_FILE
    try:
        try:
            if json.loads(path.read_text(encoding="utf-8")) == doc:
                return True
        except (OSError, ValueError):
            pass
        _write(path, doc)
        return True
    except OSError as exc:
        log.warning("the key reminder could not be queued: %s", exc)
        return False


def read_json(path: Path):
    """A queued file's content, or None for anything that is not a small regular JSON file (never followed
    through a link: the relay is root and the directory belongs to the bot's user)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_FILE + 1)
        return json.loads(data) if len(data) <= MAX_FILE else None
    except (OSError, ValueError):
        return None


def waiting_alerts(cfg) -> list[Path]:
    try:
        if directory(cfg).is_symlink():  # the bot's user owns the directory: it is never followed
            return []
        return sorted(p for p in directory(cfg).glob("alert-*.json") if p.is_file() and not p.is_symlink())
    except OSError:
        return []
