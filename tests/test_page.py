"""The page: built from the records alone, plain words, no addresses, refreshed by the commands that change it."""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

from godspeed_crypto_bot import bot, cli, config, page, records
from godspeed_crypto_bot.venue.simulated import SimulatedVenue

from .conftest import NOW, TODAY, FakeMarket


def at(day, h=0, m=2):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def two_days(cfg, store, notifier, paper):
    bot.run_daily(cfg, paper, store, notifier, now=at(TODAY))
    tomorrow = TODAY + timedelta(days=1)
    m2 = FakeMarket(today=tomorrow)
    v2 = SimulatedVenue(m2, store, "book", start_usdc=2000.0, now=lambda: at(tomorrow).timestamp())
    bot.run_daily(cfg, v2, store, notifier, now=at(tomorrow))
    return v2


@pytest.fixture
def paper_cfg(cfg):
    cfg.mode = "paper"
    return cfg


@pytest.fixture
def pstore(paper_cfg):
    s = records.Store(paper_cfg.db_path, "paper", "mainnet")
    yield s
    s.close()


@pytest.fixture
def pnotifier(paper_cfg, pstore, calls):
    from godspeed_crypto_bot.notify import Notifier
    return Notifier(paper_cfg, pstore)


@pytest.fixture
def ppaper(pstore, market):
    return SimulatedVenue(market, pstore, "book", start_usdc=2000.0, now=lambda: NOW.timestamp())


def test_every_fill_is_a_trade_and_the_replay_lands_on_the_recorded_account(paper_cfg, pstore, pnotifier, ppaper):
    v2 = two_days(paper_cfg, pstore, pnotifier, ppaper)
    tr = page.load_track(paper_cfg, "main")
    filled = pstore.db.execute("SELECT COUNT(*) FROM orders WHERE filled > 0").fetchone()[0]
    assert filled >= 4 and len(tr.trades) == filled
    # what the simulated exchange realized on each fill is what the page says each trade realized
    sim = pstore.db.execute("SELECT COALESCE(SUM(closed_pnl), 0) FROM venue_fills").fetchone()[0]
    assert sum(t.realized for t in tr.trades) == pytest.approx(sim, abs=1e-6)
    assert sum(t.fee for t in tr.trades) == pytest.approx(
        pstore.db.execute("SELECT SUM(fee) FROM orders WHERE filled > 0").fetchone()[0], abs=1e-9)
    # the balance after the last trade is the account the bot recorded right after its trades
    after = pstore.db.execute("SELECT equity FROM equity WHERE book='account_after_trades' ORDER BY day DESC "
                              "LIMIT 1").fetchone()[0]
    assert tr.trades[-1].balance_after == pytest.approx(after, rel=1e-3)
    # the holdings are what the account holds, each with its half of the strategy
    acct = v2.account()
    held = {(h["coin"], h["market"]): h["size"] for h in tr.holdings}
    assert held[("SOL", "spot")] == pytest.approx(acct.spot_size("USOL"))
    assert held[("BTC", "perp")] == pytest.approx(acct.perp_size("BTC"))
    sol = next(h for h in tr.holdings if h["coin"] == "SOL" and h["market"] == "perp")
    assert sol["carry"] < 0


def test_the_page_speaks_plainly_and_names_no_address(paper_cfg, pstore, pnotifier, ppaper):
    two_days(paper_cfg, pstore, pnotifier, ppaper)
    paper_cfg.master_address = "0x" + "ab" * 20  # even if configured, never on the page
    text = page.render(page.load_track(paper_cfg, "main"), None, now=at(TODAY + timedelta(days=1), 7))
    assert "—" not in text and "–" not in text
    assert not re.search(r"0x[0-9a-fA-F]{40}", text)
    for jargon in ("notional", "bps", " perp ", "szi"):
        assert jargon not in text.lower()
    assert "Pretend money on real prices" in text and "2,000.00 USDC" in text
    assert "What the backtest would have made" in text and "<svg" in text
    assert "perpetual contract" in text and "carry" in text and "trend half" in text
    assert re.search(r"Every trade \(\d+\), newest first", text)
    assert "<tfoot><tr><td>Total</td>" in text


def test_replay_realizes_on_the_closed_part_and_flips_the_entry():
    fills = [
        {"time_ms": 1000, "coin": "BTC", "order_coin": "BTC", "market": "perp", "venue_coin": "BTC", "side": "buy",
         "px": 100.0, "sz": 1.0, "fee": 0.1, "fee_token": "USDC", "tid": "1", "order_id": 1, "purpose": "rebalance",
         "pair": None},
        {"time_ms": 2000, "coin": "BTC", "order_coin": "BTC", "market": "perp", "venue_coin": "BTC", "side": "sell",
         "px": 110.0, "sz": 2.0, "fee": 0.2, "fee_token": "USDC", "tid": "2", "order_id": 2, "purpose": "rebalance",
         "pair": "BTC-pair"},
        {"time_ms": 2001, "coin": "@142", "order_coin": "BTC", "market": "spot", "venue_coin": "UBTC", "side": "buy",
         "px": 110.0, "sz": 1.0, "fee": 0.001, "fee_token": "UBTC", "tid": "3", "order_id": 3,
         "purpose": "rebalance", "pair": "BTC-pair"},
    ]
    trades, st = page.replay(1000.0, fills, [(1500, "BTC", 0.5)], [])
    t1, t2, t3 = trades
    assert t1.realized == 0 and t1.balance_after == pytest.approx(999.9)
    assert t2.realized == pytest.approx(10.0) and t2.before == 1 and t2.after == -1
    assert st["perp"]["BTC"] == [-1.0, 110.0]
    assert st["spot"]["BTC"][0] == pytest.approx(0.999)  # the spot fee came out of the coin
    assert t3.fee == pytest.approx(0.11) and t3.token == "UBTC"
    assert st["cash"] == pytest.approx(1000 - 0.1 + 0.5 + 10 - 0.2 - 110)
    assert "closed the bet on Bitcoin rising and opened a bet on it falling" in page.trade_sentence(t2)
    assert "real Bitcoin (held as the token UBTC)" in page.trade_sentence(t3)


def test_the_practice_exchange_section_says_the_carry_half_is_off(cfg, tmp_path):
    t = config.Config(mode="live", network="testnet", state_dir=str(tmp_path / "t"),
                      spot_tokens={"ETH": "UETH"}, allow_missing_spot=True).validate()
    s = records.Store(t.db_path, "live", "testnet")
    s.put("start", {"day": "2026-10-17", "ts_ms": 0, "account_value": 997.63, "capital": 997.63})
    s.put_equity("2026-10-18", "account", 1001.88, {"at": "2026-10-19T00:02:05+00:00"})
    s.close()
    text = page.render(None, page.load_track(t, "practice"))
    assert "The practice exchange" in text and "carry half is switched off" in text
    assert "1,001.88 USDC" in text and "+$4.25" in text


def test_the_file_is_replaced_whole_and_readable_by_the_web_server(tmp_path):
    p = page.write(tmp_path / "out", "<p>one</p>")
    p = page.write(tmp_path / "out", "<p>two</p>")
    assert p.read_text(encoding="utf-8") == "<p>two</p>"
    assert [f.name for f in (tmp_path / "out").iterdir()] == [page.PAGE_NAME]
    if sys.platform != "win32":
        assert os.stat(p).st_mode & 0o777 == 0o644


def test_halt_and_a_busy_guard_refresh_the_page_a_quiet_guard_does_not(paper_cfg, pstore, ppaper, tmp_path,
                                                                       monkeypatch):
    conf = tmp_path / "c.toml"
    conf.write_text(f'mode = "paper"\nstate_dir = "{paper_cfg.state_path.as_posix()}"\n'
                    f'confirm_dir = "{tmp_path.as_posix()}"\n')
    out = tmp_path / "page"
    out.mkdir()
    monkeypatch.setattr(page, "PAGE_DIR", out)
    monkeypatch.setattr(config, "DEFAULT_CONFIG", conf)
    monkeypatch.setattr(page, "TESTNET_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(bot, "build_venue", lambda cfg, store, market=None: ppaper)
    monkeypatch.setattr("shutil.which", lambda c: None)
    f = out / page.PAGE_NAME
    assert cli.main(["--config", str(conf), "guard"]) == 0
    assert not f.exists()  # nothing happened, nothing redrawn
    assert cli.main(["--config", str(conf), "halt", "--reason", "testing the page"]) == 0
    assert "Trading is stopped" in f.read_text(encoding="utf-8") and "testing the page" in f.read_text(encoding="utf-8")
    f.unlink()
    assert cli.main(["--config", str(conf), "resume"]) == 0
    assert "Trading is stopped" not in f.read_text(encoding="utf-8")
    f.unlink()
    pstore.add_guard_action("transfer_to_perp", None, 5.0, True, {"reason": "margin"})
    s = records.Store(paper_cfg.db_path, "paper", "mainnet")
    before = cli._activity(paper_cfg, s)
    s.add_guard_action("peg_exit", "SOL", 0.031, True, {})
    assert cli._activity(paper_cfg, s) != before
    s.close()


def test_the_page_command_writes_where_it_is_told(paper_cfg, pstore, pnotifier, ppaper, tmp_path, monkeypatch, capsys):
    two_days(paper_cfg, pstore, pnotifier, ppaper)
    conf = tmp_path / "c.toml"
    conf.write_text(f'mode = "paper"\nstate_dir = "{paper_cfg.state_path.as_posix()}"\n')
    monkeypatch.setattr(page, "TESTNET_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr("shutil.which", lambda c: None)
    assert cli.main(["--config", str(conf), "page", "--out-dir", str(tmp_path / "o")]) == 0
    text = (tmp_path / "o" / page.PAGE_NAME).read_text(encoding="utf-8")
    assert "Account value" in text and "Every trade" in text


def test_a_holding_for_one_half_says_so_once(paper_cfg, pstore, pnotifier, ppaper):
    two_days(paper_cfg, pstore, pnotifier, ppaper)
    text = page.holdings(page.load_track(paper_cfg, "main"))
    assert "The trend half: it wants a bet on rising" in text
    assert "The trend half. The trend half" not in text


@pytest.mark.parametrize("tz_env, target, want", [
    ("", "/usr/share/zoneinfo/America/New_York", "New York"),
    ("", "/usr/share/zoneinfo/posix/Europe/Paris", "Paris"),
    ("Asia/Tokyo", "", "Tokyo"),
    (":Australia/Sydney", "", "Sydney"),
    ("CET-1CEST,M3.5.0,M10.5.0/3", "", ""),  # a POSIX rule names no place
    ("", "/etc/localtime", ""),  # a copied file, not a link
    ("", "", ""),
])
def test_the_place_is_named_only_when_a_zone_name_says_it(tz_env, target, want):
    assert page.zone_name(tz_env, target) == want


def test_utc_shows_alone_and_any_other_zone_beside_it():
    assert page.server_zone(env={}, localtime="/nonexistent", offsets=(0, 0)) is None
    assert page.server_zone(env={"TZ": "America/New_York"}, offsets=(18000, 14400)) == (None, "in New York",
                                                                                       "New York time")
    assert page.server_zone(env={"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"}, offsets=(-3600, -7200)) == (
        None, "server time", "the server's time")
    assert page.server_zone(env={}, localtime="/nonexistent", offsets=(0, -3600)) is not None  # UTC in winter only


def test_the_page_shows_the_server_time_zone_or_utc_alone_never_a_fixed_city(paper_cfg, pstore, pnotifier, ppaper,
                                                                           monkeypatch):
    two_days(paper_cfg, pstore, pnotifier, ppaper)
    track = page.load_track(paper_cfg, "main")
    now = at(TODAY + timedelta(days=1), 7)
    monkeypatch.setattr(page, "ZONE", None)
    text = page.render(track, None, now=now)
    assert "All times are UTC. " in text and " in brackets" not in text and "Berlin" not in text
    assert page.when(now.timestamp()).endswith("07:02 UTC")  # 14 October, UTC alone
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        new_york = zoneinfo.ZoneInfo("America/New_York")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone data on this computer")
    monkeypatch.setattr(page, "ZONE", (new_york, "in New York", "New York time"))
    text = page.render(track, None, now=now)
    assert "All times are UTC, with New York time in brackets." in text and "Berlin" not in text
    assert page.when(now.timestamp()).endswith("07:02 UTC (03:02 in New York)")  # summer time in October


def test_without_a_name_the_hours_are_the_systems_own_local_time(monkeypatch):
    monkeypatch.setattr(page, "ZONE", (None, "server time", "the server's time"))
    ts = at(TODAY, 7).timestamp()
    local = datetime.fromtimestamp(ts).astimezone()
    assert page.when(ts).endswith(f"07:02 UTC ({local:%H:%M} server time)")
    assert page.times_note() == "All times are UTC, with the server's time in brackets."
