"""Every command is recorded with its slot, latency and outcome (the invocation records)."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from godspeed_crypto_bot import bot, cli, records
from godspeed_crypto_bot.names import NAME


def test_every_command_is_recorded_with_its_slot_latency_and_outcome(cfg, store, paper, tmp_path, monkeypatch):
    conf = tmp_path / "c.toml"
    conf.write_text(f'mode = "paper"\nstate_dir = "{cfg.state_path.as_posix()}"\nconfirm_dir = "{tmp_path.as_posix()}"\n')
    monkeypatch.setattr(bot, "build_venue", lambda cfg, store, market=None: paper)
    monkeypatch.setattr("shutil.which", lambda c: None)
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_UNIT", f"{NAME}-guard")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_EVERY", "300")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_OFFSET", "30")
    assert cli.main(["--config", str(conf), "guard"]) == 0
    monkeypatch.delenv("GODSPEED_CRYPTO_BOT_EVERY")
    assert cli.main(["--config", str(conf), "status"]) == 0
    s = records.Store(cfg.db_path, "paper", "mainnet")
    rows = [dict(r) for r in s.db.execute("SELECT * FROM invocations ORDER BY id")]
    s.close()
    g, st = rows
    assert g["command"] == "guard" and g["unit"] == f"{NAME}-guard" and g["exit_code"] == 0
    assert g["every_s"] == 300 and (g["scheduled_at"] - 30) % 300 == 0
    assert -cli.EARLY_S <= g["main_start"] - g["scheduled_at"] < 300 - cli.EARLY_S and g["finished_at"] >= g["main_start"]
    assert json.loads(g["outcome"])["status"] == "ok"
    assert st["command"] == "status" and st["scheduled_at"] is None and st["outcome"] is None


def test_a_start_a_hair_before_its_slot_belongs_to_that_slot(monkeypatch):
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_EVERY", "300")
    monkeypatch.setenv("GODSPEED_CRYPTO_BOT_OFFSET", "150")  # the testnet guard: *:02/5:30
    slot = datetime(2026, 10, 17, 21, 27, 30, tzinfo=timezone.utc).timestamp()
    assert cli._schedule(slot - 0.4) == (300, slot)  # seen on the VPS: /proc put the start 0.4 s early
    assert cli._schedule(slot + 290) == (300, slot)
    assert cli._schedule(slot + 299) == (300, slot + 300)  # 1 s before the next slot is the next slot
    assert cli._schedule(slot - 6) == (300, slot - 300)


def test_a_failed_command_is_recorded_with_its_error(cfg, tmp_path, monkeypatch):
    conf = tmp_path / "c.toml"
    conf.write_text(f'mode = "paper"\nstate_dir = "{cfg.state_path.as_posix()}"\n')

    def boom(*a, **k):
        raise ConnectionError("api.hyperliquid.xyz unreachable")

    monkeypatch.setattr(bot, "build_venue", boom)
    monkeypatch.setattr("shutil.which", lambda c: None)
    with pytest.raises(ConnectionError):
        cli.main(["--config", str(conf), "guard"])
    s = records.Store(cfg.db_path, "paper", "mainnet")
    r = dict(s.db.execute("SELECT * FROM invocations").fetchone())
    s.close()
    assert r["exit_code"] == 1 and "unreachable" in r["error"] and r["unit"] == "by hand"


def test_alerts_recorded_before_carry_no_mode_and_get_the_columns(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts REAL NOT NULL, key TEXT NOT NULL, lane TEXT NOT NULL, "
                "summary TEXT NOT NULL, act TEXT, exit_code INTEGER, detail TEXT)")
    con.execute("INSERT INTO alerts(ts, key, lane, summary) VALUES (1, 'k', 'card', 'old')")
    con.commit()
    con.close()
    s = records.Store(db, "paper", "mainnet")
    s.add_alert("k", "card", "new", "act", 0)
    rows = [dict(r) for r in s.db.execute("SELECT key, mode, network FROM alerts ORDER BY id")]
    assert rows == [{"key": "k", "mode": None, "network": None}, {"key": "k", "mode": "paper", "network": "mainnet"}]
    assert s.alerted_since("k", 0) and not records.Store(db, "dry-run", "mainnet").alerted_since("k", 0)
    s.close()
