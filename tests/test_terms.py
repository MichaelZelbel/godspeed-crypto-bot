"""The weekly terms check: the baseline passes, a changed clause stops new positions and alerts once."""

from datetime import timedelta

from godspeed_crypto_bot import bot, safety, terms

from .conftest import TODAY, FakeMarket
from .test_bot import daily

CHUNK = ("var a=1;(0,o.jsx)(`span`,{children:`Last updated on June 15, 2026`});(0,o.jsx)(n,{children:`"
         + terms.BASELINE["clause"] + "`})")
HTML = '<html><script type="module" crossorigin src="/assets/index-AAA.js"></script></html>'
INDEX = 'import"./x.js";const r={a:()=>import("assets/Other-1.js"),b:()=>import("assets/TermsOfUse-ZZ.js")};'


class Web:
    def __init__(self, chunk=CHUNK, fail=False):
        self.chunk, self.fail, self.got = chunk, fail, []

    def __call__(self, url):
        self.got.append(url)
        if self.fail:
            raise ConnectionError("down")

        class R:
            status_code = 200
            encoding = None

        r = R()
        r.text = HTML if url.endswith("/terms") else (INDEX if "index-AAA" in url else
                                                       (self.chunk if "TermsOfUse" in url else "nothing"))
        return r


def test_terms_parser_reads_the_date_and_the_clause():
    t = terms.fetch_terms(get=Web())
    assert t["date"] == "June 15, 2026" and t["sha256"] == terms.BASELINE["sha256"]
    assert t["chunk_url"].endswith("/assets/TermsOfUse-ZZ.js")


def test_unchanged_terms_change_nothing(store, notifier, calls):
    out = terms.check_terms(store, notifier, fetch=lambda: terms.fetch_terms(get=Web()))
    assert out["status"] == "unchanged" and not calls.argv and not store.get("entry_block")


def test_changed_terms_block_new_positions_and_alert_once_without_flattening(cfg, store, notifier, calls, paper):
    daily(cfg, paper, store, notifier)
    held = paper.account().perp_size("BTC")
    new_clause = terms.BASELINE["clause"].replace("Ontario, Canada", "Ontario, Canada, or the European Union")
    changed = CHUNK.replace(terms.BASELINE["clause"], new_clause).replace("June 15, 2026", "November 1, 2026")
    fetch = lambda: terms.fetch_terms(get=Web(changed))  # noqa: E731
    out = terms.check_terms(store, notifier, fetch=fetch)
    assert out["status"] == "changed" and store.get("entry_block")["terms"]
    assert len(calls.argv) == 1 and "European Union" in calls.texts()[0]  # the new clause itself, quoted
    assert "still allowed to use Hyperliquid" in calls.texts()[0] and "Germany" not in calls.texts()[0]
    terms.check_terms(store, notifier, fetch=fetch)
    assert len(calls.argv) == 1  # one alert, not one a week
    assert paper.account().perp_size("BTC") == held and not bot.halted(cfg)  # nothing flattened
    # the next daily run opens nothing new: with prices up, trend would add; it may only hold or reduce
    before = paper.account()
    paper.market = FakeMarket(today=TODAY + timedelta(days=1), drift=0.006)
    paper._mids = None
    out = daily(cfg, paper, store, notifier, day=TODAY + timedelta(days=1))
    acct = paper.account()
    for coin in ("BTC", "ETH", "SOL"):
        assert abs(acct.perp_size(coin)) <= abs(before.perp_size(coin)) + 1e-9
    assert acct.spot_size("USOL") <= before.spot_size("USOL") + 1e-9
    assert out["entry_block"]["terms"]
    assert terms.accept_terms(store)["date"] == "November 1, 2026"
    assert not store.get("entry_block")


def test_terms_fetch_failing_three_weeks_alerts_once(store, notifier, calls):
    fetch = lambda: terms.fetch_terms(get=Web(fail=True))  # noqa: E731
    for _ in range(2):
        assert terms.check_terms(store, notifier, fetch=fetch)["status"] == "failed"
    assert not calls.argv
    terms.check_terms(store, notifier, fetch=fetch)
    terms.check_terms(store, notifier, fetch=fetch)
    assert len(calls.argv) == 1 and "3 weeks" in calls.texts()[0]
    assert terms.check_terms(store, notifier, fetch=lambda: terms.fetch_terms(get=Web()))["status"] == "unchanged"
    assert store.get("terms")["failures"] == 0


def test_no_new_exposure_allows_reductions_only():
    from godspeed_crypto_bot.venue.base import AccountState, Position

    from .conftest import INSTRUMENTS

    acct = AccountState(1, 1, 0, 0, {"BTC": Position("BTC", 0.01), "ETH": Position("ETH", -1.0)},
                        {"USOL": (5.0, 0.0)})
    targets = {"BTC": (0.02, 0.0), "ETH": (0.5, 0.0), "SOL": (-8.0, 8.0)}
    out, notes = safety.restrict_targets(targets, acct, INSTRUMENTS, no_new_exposure=True)
    assert out["BTC"] == (0.01, 0.0)  # no growth
    assert out["ETH"] == (0.0, 0.0)  # no flip: flat instead
    assert out["SOL"][1] == 5.0  # spot capped at what is held
    assert notes




def test_a_second_revision_with_the_same_clause_but_a_new_date_is_told_again(store, notifier, calls):
    first = CHUNK.replace("June 15, 2026", "July 1, 2026")
    terms.check_terms(store, notifier, fetch=lambda: terms.fetch_terms(get=Web(first)))
    assert len(calls.argv) == 1
    terms.accept_terms(store)
    second = CHUNK.replace("June 15, 2026", "August 9, 2026")
    out = terms.check_terms(store, notifier, fetch=lambda: terms.fetch_terms(get=Web(second)))
    assert out["status"] == "changed" and len(calls.argv) == 2 and "August 9, 2026" in calls.texts()[1]
