"""Key generation and the live gate; the weekly terms check; the tax export; the weekly comparison report."""

import csv
import io
import re
from datetime import timedelta

import eth_account
import pytest

from godspeed_crypto_bot import eligibility, export, keys, report
from godspeed_crypto_bot.config import CREDENTIAL_PREFIX

from .conftest import TODAY, FakeMarket
from .test_bot import at, daily

# ---- keys


class FakeCreds:
    """Stands in for systemd-creds: 'encrypts' by reversing the bytes, so the file never holds the key."""

    def __init__(self):
        self.files = {}

    def encrypt(self, name, secret, out):
        out.write_bytes(b"ENC:" + secret[::-1])
        self.files[name] = out

    def decrypt(self, name, path):
        return path.read_bytes()[4:][::-1]


def test_keygen_stores_the_key_encrypted_and_returns_only_the_address(cfg, capsys):
    fc = FakeCreds()
    address = keys.keygen(cfg, "testnet", encrypt=fc.encrypt, decrypt=fc.decrypt)
    assert address.startswith("0x") and len(address) == 42
    stored = keys.Path(cfg.credstore_dir) / f"{CREDENTIAL_PREFIX}.testnet"  # where the units import it from
    secret = fc.decrypt(None, stored).decode()
    assert eth_account.Account.from_key(secret).address == address
    assert secret.encode() not in stored.read_bytes()  # not in plain text at rest
    assert keys.address_path(cfg, "testnet").read_text().strip() == address
    assert secret not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        keys.keygen(cfg, "testnet", encrypt=fc.encrypt, decrypt=fc.decrypt)  # never silently replaced


def test_keygen_keeps_nothing_if_the_round_trip_fails(cfg):
    fc = FakeCreds()
    with pytest.raises(RuntimeError):
        keys.keygen(cfg, "mainnet", encrypt=fc.encrypt, decrypt=lambda n, p: b"something else")
    assert not (keys.Path(cfg.credstore_dir) / f"{CREDENTIAL_PREFIX}.mainnet").exists()


def _live_cfg(cfg, tmp_path):
    cfg.mode, cfg.master_address = "live", "0x" + "ab" * 20
    cfg.cap_named = True
    eligibility.record(cfg, restricted=False)
    creds = tmp_path / "creds"
    creds.mkdir(exist_ok=True)
    acct = eth_account.Account.create()
    (creds / cfg.credential_name).write_text("0x" + acct.key.hex().removeprefix("0x"))
    return acct.address


def test_live_needs_mode_key_master_and_the_exact_confirmation(cfg, tmp_path):
    agent = _live_cfg(cfg, tmp_path)
    problems = keys.live_problems(cfg)
    assert len(problems) == 1 and "confirmation file" in problems[0]
    confirm = tmp_path / "LIVE-CONFIRMED.mainnet"
    assert cfg.confirm_file == confirm
    confirm.write_text("yes please")
    assert "does not match" in keys.live_problems(cfg)[0]
    confirm.write_text(keys.confirmation_text(cfg, agent))
    assert keys.live_problems(cfg) == []
    cfg.capital_cap_usdc = 5000  # a different cap is a different approval
    assert keys.live_problems(cfg)
    cfg.capital_cap_usdc = 2000
    key = (tmp_path / "creds" / cfg.credential_name).read_text()
    cfg.network = "testnet"  # and so is another network: its own file, which does not exist
    (tmp_path / "creds" / cfg.credential_name).write_text(key)
    assert cfg.confirm_file == tmp_path / "LIVE-CONFIRMED.testnet"
    assert any("does not exist" in p for p in keys.live_problems(cfg))
    (tmp_path / "LIVE-CONFIRMED.testnet").write_text(confirm.read_text())  # the mainnet text names mainnet
    assert any("does not match" in p for p in keys.live_problems(cfg))


def test_a_testnet_confirmation_can_never_unlock_mainnet(cfg, tmp_path):
    agent = _live_cfg(cfg, tmp_path)
    key = (tmp_path / "creds" / cfg.credential_name).read_text()
    cfg.network = "testnet"
    (tmp_path / "creds" / cfg.credential_name).write_text(key)
    (tmp_path / "LIVE-CONFIRMED.testnet").write_text(keys.confirmation_text(cfg, agent))
    assert keys.live_problems(cfg) == []
    cfg.network = "mainnet"
    assert any("LIVE-CONFIRMED.mainnet does not exist" in p.replace("\\", "/") for p in keys.live_problems(cfg))
    # Even copied to the mainnet path, a testnet text does not match mainnet.
    (tmp_path / "LIVE-CONFIRMED.mainnet").write_text((tmp_path / "LIVE-CONFIRMED.testnet").read_text())
    assert any("does not match" in p for p in keys.live_problems(cfg))


# ---- export and report


def test_export_has_every_fill_fee_funding_and_transfer_with_ids_and_blank_eur(cfg, store, notifier, paper):
    daily(cfg, paper, store, notifier)
    paper.sync_funding(int(at(TODAY, 3).timestamp() * 1000))
    store.add_venue_fill(1791900000000, "BTC", "perp", "BTC", "buy", 83000.0, 0.001, 0.037, "USDC", 0.0,
                         "Open Long", "0xhash1", 123, 456, "0xcl")
    store.add_funding(1791903600000, "BTC", -0.01, 0.001, 0.0000125, "userFunding", "0xhash2")
    store.add_venue_ledger(1791907200000, "deposit", 2000.0, "0xhash3:deposit", "0xhash3")
    text, n = export.to_csv(store)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == export.COLUMNS and len(rows) == n
    cats = {r["category"] for r in rows}
    assert cats >= {"trade", "fee", "funding", "transfer", "deposit"}
    assert all(r["eur_rate"] == "" and r["eur_value"] == "" for r in rows)
    assert all(r["timestamp_utc"].endswith("Z") for r in rows)
    real = [r for r in rows if r["venue_tx_hash"] == "0xhash1"]
    assert real[0]["venue_order_id"] == "123" and real[0]["venue_trade_id"] == "456"
    assert any(r["venue_tx_hash"] == "0xhash2" and r["category"] == "funding" for r in rows)
    sims = [r for r in rows if r["category"] == "trade" and r["venue_trade_id"].startswith("sim-")]
    assert sims and all(r["venue_tx_hash"] == "" for r in sims)
    text2, _ = export.to_csv(store, start=TODAY + timedelta(days=5))
    assert len(list(csv.DictReader(io.StringIO(text2)))) == 0


def test_weekly_report_compares_the_account_with_the_backtest(cfg, store, notifier, paper, tmp_path):
    daily(cfg, paper, store, notifier)
    day2 = TODAY + timedelta(days=1)
    paper.market = FakeMarket(today=day2)
    paper._mids = None
    daily(cfg, paper, store, notifier, day=day2)
    doc = report.compare(store)
    assert doc["days"] == 2 and doc["account_return"] is not None and doc["shadow_return"] is not None
    assert doc["fills"]["perp"]["orders"] >= 2 and doc["fills"]["perp"]["model_slippage_bps"] is not None
    path, text = report.weekly(cfg, store, today=day2, out_dir=tmp_path / "r")
    assert path.exists() and "Backtest assumptions on the same days" in text
    # The research the strategy came from is private: the person reads about the backtest, never a "lab".
    assert "the backtest's assumptions" in text and not re.search(r"\blab\b|\blab's\b", text, re.I)
