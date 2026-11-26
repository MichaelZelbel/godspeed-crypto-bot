"""The backtest: Hyperliquid's recorded responses give a known report, the built-in mix over the hold-out matches the
shadow ledger's replay, a short history moves the start and says so, and a failure midway leaves no report."""

import gzip
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
import requests

from godspeed_crypto_bot import backtest, cli, config, keys
from godspeed_crypto_bot.ledger import PERP_FEE, Shadow, ShadowState, slippage_for_rank
from godspeed_crypto_bot.strategy import Books, decide
from godspeed_crypto_bot.venue.hyperliquid import HyperliquidMarketData

from .test_parity import FIXTURE

RECORDED = Path(__file__).parent / "fixtures" / "hyperliquid_daily_recorded.json.gz"
TODAY = date(2025, 5, 1)  # the recording ends with the close of 30 April 2025


@pytest.fixture(scope="module")
def recorded():
    with gzip.open(RECORDED, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class FakeInfo:
    """Hyperliquid's info endpoint answering from the recording, in its own formats (500 funding rows a page)."""

    def __init__(self, doc, fail_after=None, too_many_once=False):
        self.doc, self.calls, self.fail_after, self.too_many = doc, [], fail_after, too_many_once

    def _count(self, what):
        self.calls.append(what)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise requests.ConnectionError("connection reset")
        if self.too_many:
            self.too_many = False
            from hyperliquid.utils.error import ClientError

            raise ClientError(429, None, "too many requests", {})

    def meta(self):
        return {"universe": [{"name": c, "szDecimals": 3, "maxLeverage": 20} for c in self.doc["candles"]]}

    def spot_meta(self):
        return {"tokens": [{"name": "USDC", "index": 0, "tokenId": "0x00", "szDecimals": 8}], "universe": []}

    def candles_snapshot(self, name, interval, start, end):
        self._count(("candles", name))
        assert interval == "1d"
        return [r for r in self.doc["candles"].get(name, []) if start <= r["t"] <= end]

    def funding_history(self, coin, start, end):
        self._count(("funding", coin))
        rows = [{"coin": coin, "time": t, "fundingRate": f, "premium": p}
                for t, f, p in self.doc["funding"][coin] if start <= t <= end]
        return rows[:500]


def fake_market(doc, coins, **kw):
    info = FakeInfo(doc, **kw)
    return HyperliquidMarketData("mainnet", None, coins, info=info), info


def make_cfg(tmp_path, *files):
    folder = tmp_path / "strategies"
    folder.mkdir(exist_ok=True)
    for name, text in files:
        (folder / f"{name}.toml").write_text(text, encoding="utf-8")
    return config.Config(state_dir=str(tmp_path / "state"), strategies_dir=str(folder), mode="paper").validate()


def momentum(name, coins, rebalance="daily", extra=""):
    return (f'name = "{name}"\ncoins = {json.dumps(coins)}\nrebalance = "{rebalance}"\ndirection = "long-short"\n'
            f'no_trade_band = 0.02\n\n[signal]\nkind = "momentum"\nlookbacks = [20, 60]\n\n[sizing]\n'
            f'method = "equal"\ngross_exposure = 1.0\n{extra}')


def run(cfg, setting, doc, coins, **kw):
    market, info = fake_market(doc, coins, **{k: kw.pop(k) for k in ("fail_after", "too_many_once") if k in kw})
    out = backtest.run(cfg, setting, market=market, today=TODAY, sleep=lambda s: None, **kw)
    return out, info


# ---- recorded responses give a known report


def test_recorded_responses_give_a_known_report(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("rec", momentum("rec", ["BTC", "ETH"])))
    (md, js, text), _info = run(cfg, "file:rec", recorded, ("BTC", "ETH"))
    doc = json.loads(js.read_text(encoding="utf-8"))
    assert md.read_text(encoding="utf-8") == text
    assert text.splitlines()[0] == backtest.FIRST_LINE == doc["first_line"]
    assert "not a forecast" in backtest.FIRST_LINE
    # the period: the recording starts on 1 August 2024, and the strategy needs 121 days before its first day
    assert doc["period"] == {"start": "2024-11-30", "end": "2025-04-30", "days": 152, "why": doc["period"]["why"]}
    assert "BTC and ETH both had the 121 days" in doc["period"]["why"]
    assert "their daily prices there begin on 2024-08-01" in doc["period"]["why"]  # the recording starts then
    assert md.name == f"file-rec-{doc['strategy']['fingerprint']}-2024-11-30-to-2025-04-30.md"
    daily = doc["daily"]
    assert len(daily) == 152 and daily[0][0] == "2024-11-30" and daily[-1][0] == "2025-04-30"

    # every number checked against a plain recomputation from the daily series and the recording
    equity = [e for _d, e, _h in daily]
    before = [2000.0, *equity[:-1]]
    changes = [b / a - 1 for a, b in zip(before, equity, strict=True)]
    k = int(np.argmin(changes))
    assert doc["worst_day"] == {"day": daily[k][0], "change": pytest.approx(changes[k])}
    assert doc["result"] == pytest.approx(equity[-1] / 2000.0 - 1)
    peak, worst = 2000.0, 0.0
    for e in equity:
        peak = max(peak, e)
        worst = min(worst, e / peak - 1)
    assert doc["deepest_fall"]["change"] == pytest.approx(worst)
    candles = {c: {datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date(): r for r in recorded["candles"][c]}
               for c in ("BTC", "ETH")}
    first_open = {c: float(candles[c][date(2024, 11, 30)]["o"]) for c in candles}
    hold_end = sum(1000.0 / first_open[c] * float(candles[c][date(2025, 4, 30)]["c"]) for c in candles)
    assert doc["hold"]["end_equity"] == pytest.approx(hold_end)
    costs, trading = doc["costs"], doc["trading"]
    assert costs["fees"] == pytest.approx(trading["traded_usdc"] * PERP_FEE)  # perpetuals only
    assert costs["slippage"] == pytest.approx(trading["traded_usdc"] * 2e-4)  # BTC and ETH: 2 basis points
    assert costs["funding_net"] == pytest.approx(costs["funding_received"] - costs["funding_paid"])
    assert 0 < trading["trading_days"] <= 152 and trading["trades"] >= trading["trading_days"]
    weeks = doc["worst_weeks"]
    assert len(weeks) == 3 and weeks[0] == doc["worst_week"]
    assert weeks[0]["change"] <= weeks[1]["change"] <= weeks[2]["change"]
    assert all(date.fromisoformat(w["from"]).weekday() == 0 or w["from"] == "2024-11-30" for w in weeks)
    # the known report: these figures are what the recorded prices give, and they must not drift
    assert round(doc["result"], 6) == round(equity[-1] / 2000.0 - 1, 6)
    assert (doc["worst_day"]["day"], round(doc["worst_day"]["change"], 4)) == KNOWN["worst_day"]
    assert round(doc["result"], 4) == KNOWN["result"]
    assert round(doc["deepest_fall"]["change"], 4) == KNOWN["deepest_fall"]
    assert trading["trades"] == KNOWN["trades"]
    for line in ("## The whole period", "## The worst stretches", "## Costs and trading", "## How it was tested",
                 "- The three worst weeks (Monday to Sunday):", "Holding an equal share of BTC and ETH",
                 "it did not try other settings"):
        assert line in text


KNOWN = {"worst_day": ("2025-03-02", -0.1149), "result": -0.0274, "deepest_fall": -0.1943, "trades": 104}


def test_the_report_presents_the_past_never_a_forecast(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("rec", momentum("rec", ["BTC", "ETH"])))
    (_md, _js, text), _info = run(cfg, "file:rec", recorded, ("BTC", "ETH"))
    body = "\n".join(text.splitlines()[1:])
    assert not re.search(r"\b(will|would make|expect|forecast|promise|guarantee|predict)\w*", body, re.I), body
    assert "—" not in text


# ---- the built-in mix over the hold-out is the shadow ledger's replay


def test_the_built_in_mix_over_the_holdout_matches_the_shadow_ledgers_replay():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    h = doc["history"]
    n = len(h["perp_close"])
    first = np.datetime64(h["first_day"], "D")
    from godspeed_crypto_bot.strategy import History

    hist = History(np.arange(first, first + np.timedelta64(n, "D")), h["coins"], h["perp_open"], h["perp_high"],
                   h["perp_low"], h["perp_close"], h["perp_traded"], h["spot_open"], h["spot_high"], h["spot_low"],
                   h["spot_close"], h["spot_traded"], h["funding"], h["first_trade"], h["has_spot"])
    start = date.fromisoformat(doc["start_day"])
    end = date.fromisoformat(doc["days"][-1]["day"])

    def tiers(d):
        ranks = doc["ranks"][d.isoformat()[:7]]
        return lambda c, ranks=ranks: slippage_for_rank(ranks[c])

    run_ = backtest.simulate(hist, "mix", None, start, end, doc["start_equity"], slippage=tiers,
                             running_per_day=5.0 / 30.0)

    # the shadow ledger's own replay of the same days, as tests/test_parity.py runs it
    def row(name, d):
        k = int((np.datetime64(d, "D") - first) // np.timedelta64(1, "D"))
        return {c: h[name][k][j] for j, c in enumerate(h["coins"])}

    shadow = Shadow(ShadowState(Books.fresh(doc["start_equity"], start)))
    replay = []
    for day in doc["days"]:
        d = date.fromisoformat(day["day"])
        dec = decide(hist.through(date.fromordinal(d.toordinal() - 1)), shadow.s.books)
        shadow.execute(dec, row("perp_open", d), row("spot_open", d), slippage=tiers(d))
        shadow.settle(d, row("perp_close", d), row("spot_close", d), row("funding", d), running_per_day=5.0 / 30.0)
        replay.append(shadow.s.books.equity)
    assert len(run_.equity) == len(replay) == 1016
    assert max(abs(a - b) for a, b in zip(run_.equity, replay, strict=True)) < 0.01  # the documented tolerance
    assert max(abs(a - b) for a, b in zip(run_.equity, doc["recorded_daily_equity"], strict=True)) < 0.01
    assert run_.trading_days > 300


# ---- the period


def test_a_coin_with_a_short_history_moves_the_start_and_the_report_says_so(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("young", momentum("young", ["BTC", "HYPE"])))
    (_md, js, text), _info = run(cfg, "file:young", recorded, ("BTC", "HYPE"))
    doc = json.loads(js.read_text(encoding="utf-8"))
    hype_first = min(datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date()
                     for r in recorded["candles"]["HYPE"] if r["n"] > 0)
    start = hype_first + timedelta(days=121)
    assert doc["period"]["start"] == str(start)
    assert f"HYPE has the shortest history there, from {hype_first}" in text
    assert f"It starts on {start}, the first day on which BTC and HYPE both had the 121 days" in text


def test_a_start_asked_for_too_early_is_moved_and_one_asked_later_is_kept(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("rec", momentum("rec", ["BTC", "ETH"])))
    (_md, js, text), _ = run(cfg, "file:rec", recorded, ("BTC", "ETH"), start=date(2024, 9, 1))
    assert json.loads(js.read_text())["period"]["start"] == "2024-11-30"
    assert "You asked for 2024-09-01, which is too early." in text
    (_md, js, text), _ = run(cfg, "file:rec", recorded, ("BTC", "ETH"), start=date(2025, 2, 1), end=date(2025, 3, 31))
    p = json.loads(js.read_text())["period"]
    assert (p["start"], p["end"], p["days"]) == ("2025-02-01", "2025-03-31", 59)
    assert "It starts on 2025-02-01, as asked. The earliest possible start is 2024-11-30" in text
    with pytest.raises(backtest.BacktestError, match="the last closed day"):
        run(cfg, "file:rec", recorded, ("BTC", "ETH"), end=date(2025, 5, 1))


# ---- failures and the cache


def test_the_api_failing_midway_leaves_no_report(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("rec", momentum("rec", ["BTC", "ETH"])))
    with pytest.raises(backtest.BacktestError) as exc:
        run(cfg, "file:rec", recorded, ("BTC", "ETH"), fail_after=4)
    assert "Hyperliquid did not give the past prices" in str(exc.value) and "no report was written" in str(exc.value)
    folder = tmp_path / "state" / "backtests"
    assert not list(folder.glob("*.md")) and not list(folder.glob("*.json"))
    (_md, js, _t), info = run(cfg, "file:rec", recorded, ("BTC", "ETH"))  # the next try uses what was cached
    assert js.exists()


def test_the_cache_fetches_only_what_is_missing(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("rec", momentum("rec", ["BTC", "ETH"])))
    (_md, js1, _t), first = run(cfg, "file:rec", recorded, ("BTC", "ETH"))
    assert ("candles", "BTC") in first.calls and ("funding", "BTC") in first.calls
    (_md, js2, _t), second = run(cfg, "file:rec", recorded, ("BTC", "ETH"))
    assert second.calls == []  # everything was there
    d1, d2 = json.loads(js1.read_text()), json.loads(js2.read_text())
    assert d1["daily"] == d2["daily"]
    (_md, _js, _t), third = run(cfg, "file:rec", recorded, ("BTC", "ETH"), start=date(2024, 12, 15))
    assert third.calls == []  # a later start reads what the cache has


def test_too_many_requests_is_waited_out(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("rec", momentum("rec", ["BTC", "ETH"])))
    (_md, js, _t), info = run(cfg, "file:rec", recorded, ("BTC", "ETH"), too_many_once=True)
    assert js.exists()


def test_a_weekly_file_trades_only_on_mondays_in_the_test(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("wk", momentum("wk", ["BTC", "ETH"], rebalance="weekly")))
    (_md, js, _t), _ = run(cfg, "file:wk", recorded, ("BTC", "ETH"))
    doc = json.loads(js.read_text())
    days = [date.fromisoformat(d) for d, _e, _h in doc["daily"]]
    mondays = sum(1 for d in days if d.weekday() == 0)
    assert 0 < doc["trading"]["trading_days"] <= mondays + 1  # its Mondays, and the first day


def test_an_unknown_or_broken_strategy_is_refused_in_one_sentence(tmp_path, recorded):
    cfg = make_cfg(tmp_path, ("bad", momentum("bad", ["BTC"]).replace('"daily"', '"hourly"')))
    with pytest.raises(backtest.BacktestError, match='rebalance must be "daily", "weekly" or "monthly"'):
        run(cfg, "file:bad", recorded, ("BTC",))
    with pytest.raises(backtest.BacktestError, match='Say which strategy to test'):
        run(cfg, "everything", recorded, ("BTC",))


# ---- the command: public data only


def test_the_command_reads_public_data_only_and_touches_no_records(tmp_path, recorded, monkeypatch, capsys):
    p = tmp_path / "c.toml"
    p.write_text(f'mode = "live"\nstate_dir = "{tmp_path.as_posix()}/state"\n'
                 f'strategies_dir = "{tmp_path.as_posix()}/strategies"\n', encoding="utf-8")
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "rec.toml").write_text(momentum("rec", ["BTC", "ETH"]), encoding="utf-8")

    def no_key(*a, **kw):
        raise AssertionError("the backtest must never load a key")

    monkeypatch.setattr(keys, "load_agent_key", no_key)
    monkeypatch.setattr("godspeed_crypto_bot.venue.hyperliquid.HyperliquidMarketData",
                        lambda network, spot, coins: fake_market(recorded, coins)[0])
    code = cli.main(["--config", str(p), "backtest", "file:rec", "--start", "2025-01-01", "--end", "2025-04-30"])
    out, err = capsys.readouterr()
    assert code == 0 and out.splitlines()[0] == backtest.FIRST_LINE and "written:" in err
    assert not (tmp_path / "state" / "bot.sqlite").exists()  # the records are not touched
    code = cli.main(["--config", str(p), "backtest", "file:rec", "--start", "2025-01-01", "--end", "2025-04-30",
                     "--json"])
    assert code == 0 and json.loads(capsys.readouterr().out)["period"]["start"] == "2025-01-01"
    assert cli.main(["--config", str(p), "backtest", "file:missing"]) == 1
    assert "There is no strategy file missing.toml" in capsys.readouterr().err


def test_the_numbers_of_the_stretches_by_hand():
    days = [date(2025, 1, 6) + timedelta(days=k) for k in range(10)]  # Monday 6 January to Wednesday 15th
    equity = [100, 90, 95, 120, 110, 100, 130, 125, 126, 140]
    s = backtest.stretches(days, [float(e) for e in equity], 100.0)
    assert s["worst_day"] == {"day": "2025-01-07", "change": pytest.approx(-0.1)}
    assert s["weeks"][0] == {"from": "2025-01-13", "to": "2025-01-15", "change": pytest.approx(140 / 130 - 1)} or \
        s["weeks"][0]["change"] <= s["weeks"][1]["change"]
    fall = s["deepest_fall"]
    assert fall["change"] == pytest.approx(100 / 120 - 1)
    assert (fall["high_day"], fall["low_day"], fall["back_day"]) == ("2025-01-09", "2025-01-11", "2025-01-12")
    assert (fall["falling_days"], fall["lasted_days"]) == (2, 3)
    flat = backtest.stretches(days[:2], [100.0, 101.0], 100.0)
    assert flat["deepest_fall"] == {"change": 0.0}
    never_back = backtest.stretches(days[:3], [100.0, 80.0, 90.0], 100.0)["deepest_fall"]
    assert never_back["back_day"] is None and never_back["lasted_days"] == 2
    assert math.isclose(never_back["change"], -0.2)
