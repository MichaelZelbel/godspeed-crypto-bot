"""The strategy choice: the setting, its refusal, the plain words, and changing it on a running bot."""

from datetime import date

import numpy as np
import pytest

from godspeed_crypto_bot import config
from godspeed_crypto_bot.strategy import (
    STRATEGIES,
    STRATEGY_WORDS,
    Books,
    History,
    config_for,
    decide,
    split_equity,
)
from tests.test_parity import FIXTURE  # noqa: F401  (the same fixture file the parity test reads)


def test_the_three_choices_and_their_words():
    assert STRATEGIES == ("mix", "trend", "carry")
    assert STRATEGY_WORDS == {"mix": "half trend, half carry", "trend": "trend on BTC and ETH",
                              "carry": "carry on BTC, ETH and SOL"}


def test_default_is_the_mix():
    assert config.Config().strategy == "mix"
    assert config.load("/nonexistent/config.toml").strategy == "mix"


@pytest.mark.parametrize("value", ["trend", "carry", "mix"])
def test_valid_values_load(tmp_path, value):
    p = tmp_path / "c.toml"
    p.write_text(f'strategy = "{value}"\n')
    assert config.load(p).strategy == value


@pytest.mark.parametrize("value", ["Mix", "both", "", "s3-maj"])
def test_unknown_value_is_refused_naming_the_three_choices(tmp_path, value):
    p = tmp_path / "c.toml"
    p.write_text(f'strategy = "{value}"\n')
    with pytest.raises(ValueError) as exc:
        config.load(p)
    assert 'strategy must be "mix", "trend" or "carry"' in str(exc.value)


def test_split_and_fresh_books():
    assert split_equity("mix", 100.0) == (50.0, 50.0)
    assert split_equity("trend", 100.0) == (100.0, 0.0)
    assert split_equity("carry", 100.0) == (0.0, 100.0)
    b = Books.fresh(1000.0, date(2026, 1, 1), "carry")
    assert (b.trend.equity, b.carry.equity, b.strategy) == (0.0, 1000.0, "carry")
    assert Books.from_dict(b.to_dict()).strategy == "carry"


def test_books_saved_before_the_choice_existed_are_the_mix():
    d = Books.fresh(1000.0, date(2026, 1, 1)).to_dict()
    del d["strategy"]
    assert Books.from_dict(d).strategy == "mix"


def _history():
    import gzip
    import json

    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    h = doc["history"]
    n = len(h["perp_close"])
    dates = np.arange(np.datetime64(h["first_day"], "D"), np.datetime64(h["first_day"], "D") + np.timedelta64(n, "D"))
    hist = History(dates, h["coins"], h["perp_open"], h["perp_high"], h["perp_low"], h["perp_close"],
                   h["perp_traded"], h["spot_open"], h["spot_high"], h["spot_low"], h["spot_close"],
                   h["spot_traded"], h["funding"], h["first_trade"], h["has_spot"])
    return doc, hist


def test_changing_the_choice_rebalances_at_the_next_run():
    """A running mix holds both books; choosing "trend" moves all the equity to the trend book, closes the
    carry pairs and sizes the trend book to the whole equity, in the one decision."""
    doc, hist = _history()
    day = next(d for d in doc["days"] if d["carry"]["perp_q"] and d["trend"]["q"])
    d = date.fromisoformat(day["day"])
    from tests.test_parity import _books

    books = _books(day, None, day["trend"]["equity"], day["carry"]["equity"])
    past = hist.through(date.fromordinal(d.toordinal() - 1))
    dec = decide(past, books, config=config_for("trend"))
    assert dec.carry_equity == 0.0
    assert dec.trend_equity == pytest.approx(day["trend"]["equity"] + day["carry"]["equity"])
    assert dec.books_after.strategy == "trend"
    assert not dec.books_after.carry.perp_q and not dec.books_after.carry.spot_q
    assert set(dec.carry_orders) == set(day["carry"]["spot_q"]) and not any(dec.carry_orders.values())
    # and the next day it is simply a trend book, with no further switching
    again = decide(past, dec.books_after, config=config_for("trend"))
    assert again.books_after.strategy == "trend" and not again.carry_orders


def test_an_unknown_strategy_stops_the_command_with_one_sentence(tmp_path, capsys):
    from godspeed_crypto_bot import cli

    p = tmp_path / "c.toml"
    p.write_text('strategy = "everything"\n')
    code = cli.main(["--config", str(p), "status"])
    err = capsys.readouterr().err.strip()
    assert code == cli.EXIT_CONFIG
    assert err.count("\n") == 0 and 'strategy must be "mix", "trend" or "carry"' in err


@pytest.mark.parametrize("new", ["trend", "carry"])
def test_switching_a_running_shadow_ledger_ends_with_one_book_owning_everything(new):
    from godspeed_crypto_bot.ledger import Shadow, ShadowState, slippage_for_rank

    doc, hist = _history()
    h = doc["history"]
    first = np.datetime64(h["first_day"], "D")

    def row(name, d):
        k = int((np.datetime64(d, "D") - first) // np.timedelta64(1, "D"))
        return {c: h[name][k][j] for j, c in enumerate(h["coins"])}

    start = date.fromisoformat(doc["start_day"])
    shadow = Shadow(ShadowState(Books.fresh(2000.0, start)))
    for n, day in enumerate(doc["days"][:120]):
        d = date.fromisoformat(day["day"])
        ranks = doc["ranks"][day["day"][:7]]
        choice = "mix" if n < 60 else new  # the choice changes on day 61
        dec = decide(hist.through(date.fromordinal(d.toordinal() - 1)), shadow.s.books, config=config_for(choice))
        shadow.execute(dec, row("perp_open", d), row("spot_open", d),
                       slippage=lambda c, ranks=ranks: slippage_for_rank(ranks[c]))
        shadow.settle(d, row("perp_close", d), row("spot_close", d), row("funding", d), running_per_day=5.0 / 30.0)
        if n == 60:
            assert shadow.s.books.strategy == new
    b = shadow.s.books
    idle, run = (b.carry, b.trend) if new == "trend" else (b.trend, b.carry)
    assert idle.equity == 0.0 and run.equity > 1000.0
    assert not (b.carry.perp_q or b.carry.spot_q) if new == "trend" else not b.trend.q


def test_the_page_names_the_strategy_in_plain_words(tmp_path):
    from godspeed_crypto_bot import page

    cfg = config.Config(strategy="carry", state_dir=str(tmp_path))
    tr = page.Track(cfg, "main")
    html = page.headline(tr)
    assert "carry on BTC, ETH and SOL" in html
    assert "carry on BTC, ETH and SOL" not in page.explain("trend") and "trend" in page.explain("trend")
    assert "half" not in page.explain("carry").split("<b>The carry trade.</b>")[1].split("<p")[0]
