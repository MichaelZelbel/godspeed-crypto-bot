"""go-live: only the person's own typed sentence writes the confirmation file and switches on real money."""

import io
from datetime import date
from pathlib import Path

import eth_account
import pytest

from godspeed_crypto_bot import config, configure, eligibility, golive, keys, strategy_file


class Tty(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


MASTER = "0x" + "ab" * 20
FULL = f'mode = "paper"\nnetwork = "mainnet"\nmaster_address = "{MASTER}"\ncapital_cap_usdc = 500\n'


class Setup:
    """A configuration file, a key address, and a stand-in for the bot's own preflight."""

    def __init__(self, tmp_path, cfg):
        self.tmp = tmp_path
        self.relay_dir = cfg.relay_dir if hasattr(cfg, "relay_dir") else None
        self.path = tmp_path / "config.toml"
        self.agent = eth_account.Account.create().address
        self.preflight_calls = []
        self.preflight_exit = [0, 0]  # the pre-check, then the final check
        self.cfg = None
        self.write(FULL)

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        c = config.load(self.path)
        c.confirm_dir = str(self.tmp)
        c.credstore_dir = str(self.tmp / "credstore")
        c.state_dir = str(self.tmp / "state")
        self.cfg = c
        return c

    def run(self, argv, timeout=0):
        self.preflight_calls.append(argv)
        code = self.preflight_exit.pop(0) if self.preflight_exit else 0
        return code, "" if code == 0 else "the account is not approved yet", ""


@pytest.fixture
def s(tmp_path, cfg):
    st = Setup(tmp_path, cfg)
    keys.address_path(st.cfg).parent.mkdir(parents=True, exist_ok=True)
    keys.address_path(st.cfg).write_text(st.agent + chr(10))
    eligibility.record(st.cfg, restricted=False)
    return st


def go(s, line, tty=True, cfg=None):
    return golive.go_live(cfg or s.cfg, s.path, stdin=Tty(tty), stdout=Tty(tty), ask=lambda prompt: line,
                          run=s.run, say=lambda *a: None)


def sentence(s):
    return keys.confirmation_text(s.cfg, s.agent).strip()


def nothing_written(s):
    return not s.cfg.confirm_file.exists() and 'mode = "paper"' in s.path.read_text()


def test_the_exact_sentence_writes_the_file_and_switches_to_live(s):
    assert go(s, sentence(s)) == sentence(s)
    assert s.cfg.confirm_file.read_text() == sentence(s) + chr(10)
    assert 'mode = "live"' in s.path.read_text()
    assert [c[-1] for c in s.preflight_calls] == ["--before-confirmation", "preflight"]
    assert all(c[c.index("--mode") + 1] == "live" for c in s.preflight_calls)


def test_the_sentence_names_the_network_the_account_the_key_and_the_cap(s):
    text = sentence(s)
    assert chr(10) not in text  # one line, typed in one go
    for part in ("Crypto Bot", "mainnet", MASTER, s.agent.lower(), "500 USDC"):
        assert part in text


@pytest.mark.parametrize("wrong", ["", "yes", "i approve", "y", "I approve live trading by Crypto Bot"])
def test_a_wrong_sentence_refuses_and_writes_nothing(s, wrong):
    with pytest.raises(golive.NotDone, match="not the same sentence"):
        go(s, wrong)
    assert nothing_written(s)


def test_a_sentence_with_a_different_cap_is_wrong(s):
    with pytest.raises(golive.NotDone, match="not the same sentence"):
        go(s, sentence(s).replace("500 USDC", "5000 USDC"))
    assert nothing_written(s)


def test_input_that_is_not_a_terminal_is_refused(s):
    with pytest.raises(golive.NotDone, match="terminal"):
        go(s, sentence(s), tty=False)
    assert nothing_written(s) and s.preflight_calls == []


def test_no_cap_named_by_the_person_is_refused(s):
    c = s.write(FULL.replace("capital_cap_usdc = 500" + chr(10), ""))
    assert c.capital_cap_usdc == 2000 and not c.cap_named  # the paper default is not a live cap
    with pytest.raises(golive.NotDone, match="most the bot may use"):
        go(s, "anything", cfg=c)
    assert nothing_written(s) and s.preflight_calls == []


def test_the_live_gate_itself_refuses_without_a_named_cap(s):
    live = FULL.replace('"paper"', '"live"')
    c = s.write(live.replace("capital_cap_usdc = 500" + chr(10), ""))
    assert any("capital_cap_usdc is not set" in p for p in keys.live_problems(c))
    named = s.write(live)
    assert not any("capital_cap_usdc is not set" in p for p in keys.live_problems(named))


def test_a_failed_check_refuses_and_writes_nothing(s):
    s.preflight_exit = [1]
    with pytest.raises(golive.NotDone, match="not approved yet"):
        go(s, sentence(s))
    assert nothing_written(s)


def test_a_failed_final_check_takes_the_confirmation_back(s):
    s.preflight_exit = [0, 1]
    with pytest.raises(golive.NotDone, match="taken back"):
        go(s, sentence(s))
    assert nothing_written(s)


def test_no_key_no_account_no_answer_each_refuse(s):
    keys.address_path(s.cfg).unlink()
    with pytest.raises(golive.NotDone, match="There is no key"):
        go(s, sentence(s))
    keys.address_path(s.cfg).write_text(s.agent + chr(10))
    c = s.write(FULL.replace(f'master_address = "{MASTER}"' + chr(10), ""))
    with pytest.raises(golive.NotDone, match="master_address"):
        go(s, "x", cfg=c)
    s.write(FULL)
    eligibility.path(s.cfg).unlink()
    with pytest.raises(golive.NotDone, match="never answered"):
        go(s, "x")
    eligibility.record(s.cfg, restricted=True)
    with pytest.raises(golive.NotDone, match="does not allow"):
        go(s, "x")
    assert nothing_written(s)


def test_the_bot_never_writes_the_confirmation_file_itself():
    import inspect

    from godspeed_crypto_bot import bot, cli

    assert "confirm_file" not in inspect.getsource(bot)
    assert "write_text" not in inspect.getsource(keys).split("def keygen")[0]  # nothing before keygen writes files
    assert "golive.go_live" in inspect.getsource(cli)  # the only writer, called only by the go-live command


def test_configure_sets_values_keeps_comments_and_refuses_bad_ones(s):
    s.path.write_text(NL_JOIN('mode = "paper"  # keep me', 'network = "mainnet"', "[limits]", "leverage = 3", ""),
                      encoding="utf-8")
    configure.set_value(s.path, "capital_cap_usdc", 750)
    configure.set_value(s.path, "mode", "live")
    text = s.path.read_text()
    assert 'mode = "live"  # keep me' in text and "capital_cap_usdc = 750" in text
    assert text.index("capital_cap_usdc") < text.index("[limits]")
    with pytest.raises(ValueError):
        configure.set_value(s.path, "capital_cap_usdc", -5)
    assert "capital_cap_usdc = 750" in s.path.read_text()  # the bad value changed nothing
    with pytest.raises(ValueError):
        configure.set_value(s.path, "strategy", "everything")


def NL_JOIN(*lines):
    return chr(10).join(lines)


def test_the_eligibility_answer_is_kept_with_its_date(cfg, tmp_path):
    cfg.confirm_dir = str(tmp_path)
    assert eligibility.problems(cfg)
    eligibility.record(cfg, False, today=date(2026, 11, 26))
    assert eligibility.read(cfg)["answered_on"] == "2026-11-26" and eligibility.problems(cfg) == []
    eligibility.record(cfg, True)
    assert eligibility.problems(cfg) == [eligibility.REFUSAL]


def test_go_live_runs_as_root_from_the_persons_terminal_and_nothing_else_can_confirm():
    wrapper = (Path(__file__).resolve().parent.parent / "deploy" / "templates" / "wrapper").read_text()
    root_part = wrapper.split("case")[1].split("esac")[0]
    assert "go-live" in root_part and "@NAME@" in wrapper


def test_the_sentence_must_match_exactly_not_nearly(s):
    for off in (" " + sentence(s), sentence(s) + " ", sentence(s) + "  ", sentence(s).replace(" ", "  ", 1)):
        with pytest.raises(golive.NotDone, match="not the same sentence"):
            go(s, off)
    assert nothing_written(s)
    assert go(s, sentence(s) + chr(13) + chr(10)) == sentence(s)  # only the Enter itself is dropped


def test_a_confirmation_file_with_extra_whitespace_does_not_open_the_gate(s):
    live = s.write(FULL.replace('"paper"', '"live"'))
    agent = s.agent
    for text in (keys.confirmation_text(live, agent) + chr(10), " " + keys.confirmation_text(live, agent),
                 keys.confirmation_text(live, agent).strip()):
        live.confirm_file.write_text(text, encoding="utf-8")
        assert any("does not match" in p for p in keys_problems_with_key(live, agent))
    live.confirm_file.write_text(keys.confirmation_text(live, agent), encoding="utf-8")
    assert not any("confirmation file" in p for p in keys_problems_with_key(live, agent))


def keys_problems_with_key(cfg, agent_address, monkeypatch=None):
    """live_problems with the key lookup stood in (the real one reads systemd's credentials directory)."""
    original = keys.eth_account.Account.from_key
    keys.eth_account.Account.from_key = lambda k: type("A", (), {"address": agent_address})()
    orig_load = keys.load_agent_key
    keys.load_agent_key = lambda c: "0x" + "1" * 64
    try:
        return keys.live_problems(cfg)
    finally:
        keys.eth_account.Account.from_key = original
        keys.load_agent_key = orig_load


def test_a_renewed_key_can_be_confirmed_again_while_live(s):
    live = s.write(FULL.replace('"paper"', '"live"'))
    live.confirm_file.write_text(keys.confirmation_text(live, s.agent), encoding="utf-8")
    with pytest.raises(golive.NotDone, match="already set to trade live"):
        go(s, "x", cfg=live)
    new_agent = eth_account.Account.create().address  # keygen --replace made another key
    keys.address_path(live).write_text(new_agent + chr(10))
    s.agent = new_agent
    s.preflight_exit = [0, 0]
    assert go(s, keys.confirmation_text(live, new_agent).strip(), cfg=live) == keys.confirmation_text(live, new_agent).strip()
    assert live.confirm_file.read_text() == keys.confirmation_text(live, new_agent)


@pytest.mark.parametrize("bad", ["inf", "nan", "-inf", "0", "-5", "1e999"])
def test_a_cap_that_is_not_a_real_amount_is_refused(s, bad):
    with pytest.raises(ValueError):
        s.write(FULL.replace("capital_cap_usdc = 500", f"capital_cap_usdc = {bad}"))


def test_a_malformed_country_answer_is_not_an_answer(cfg, tmp_path):
    cfg.confirm_dir = str(tmp_path)
    for text in ("{}", "[]", '{"restricted": "no", "answered_on": "2026-11-26"}', '{"restricted": false}',
                 '{"restricted": false, "answered_on": "yesterday"}', "null", "not json"):
        eligibility.path(cfg).write_text(text, encoding="utf-8")
        assert eligibility.problems(cfg), text
    eligibility.record(cfg, False)
    assert eligibility.problems(cfg) == []


def test_there_is_no_command_that_prints_a_sentence_to_redirect_into_the_file():
    from godspeed_crypto_bot import cli

    with pytest.raises(SystemExit):
        cli.main(["confirm-text"])


def test_a_failed_final_check_puts_back_what_was_there(s):
    live = s.write(FULL.replace('"paper"', '"live"'))
    live.confirm_file.write_text("the confirmation of an earlier key", encoding="utf-8")
    s.preflight_exit = [0, 1]
    with pytest.raises(golive.NotDone, match="taken back"):
        go(s, sentence(s), cfg=live)
    assert live.confirm_file.read_text() == "the confirmation of an earlier key"


# ---- the sentence names the strategy (0.2.0)

MOMO = ('name = "momo"\ncoins = ["BTC", "ETH"]\nrebalance = "daily"\ndirection = "long-short"\n\n[signal]\n'
        'kind = "momentum"\nlookbacks = [20, 60]\n\n[sizing]\nmethod = "equal"\n')


def with_file(s, text=MOMO, mode="paper"):
    folder = s.tmp / "strategies"
    folder.mkdir(exist_ok=True)
    (folder / "momo.toml").write_text(text, encoding="utf-8")
    c = s.write(FULL.replace('"paper"', f'"{mode}"') + f'strategy = "file:momo"\nstrategies_dir = "{folder.as_posix()}"\n')
    return c


def test_the_sentence_names_the_strategy_and_a_files_fingerprint(s):
    assert sentence(s).endswith(', running the strategy "mix".')
    c = with_file(s)
    fp = strategy_file.load_for(c).fingerprint
    assert keys.confirmation_text(c, s.agent).strip().endswith(
        f', running your own strategy "momo" with the fingerprint {fp}.')
    assert go(s, keys.confirmation_text(c, s.agent).strip(), cfg=c)
    assert c.confirm_file.read_text() == keys.confirmation_text(c, s.agent)


def test_a_changed_strategy_while_live_is_refused_until_a_new_sentence(s):
    live = s.write(FULL.replace('"paper"', '"live"'))
    live.confirm_file.write_text(keys.confirmation_text(live, s.agent), encoding="utf-8")
    assert not any("confirm" in p for p in keys_problems_with_key(live, s.agent))
    switched = s.write(FULL.replace('"paper"', '"live"') + 'strategy = "trend"\n')
    problems = keys_problems_with_key(switched, s.agent)
    assert any('the strategy in use (the strategy "trend") is not the one you confirmed' in p for p in problems)
    assert any("run godspeed-crypto-bot go-live in your own terminal" in p for p in problems)
    s.preflight_exit = [0, 0]
    assert go(s, keys.confirmation_text(switched, s.agent).strip(), cfg=switched)  # the new sentence, typed
    assert not any("confirm" in p for p in keys_problems_with_key(switched, s.agent))


def test_a_changed_strategy_file_while_live_is_a_new_strategy(s):
    live = with_file(s, mode="live")
    live.confirm_file.write_text(keys.confirmation_text(live, s.agent), encoding="utf-8")
    assert not any("confirm" in p for p in keys_problems_with_key(live, s.agent))
    (s.tmp / "strategies" / "momo.toml").write_text(MOMO.replace("[20, 60]", "[10, 30]"), encoding="utf-8")
    problems = keys_problems_with_key(live, s.agent)
    assert any('is not the one you confirmed' in p and 'your own strategy "momo"' in p for p in problems)
    (s.tmp / "strategies" / "momo.toml").write_text(MOMO.replace('"daily"', '"hourly"'), encoding="utf-8")
    problems = keys_problems_with_key(live, s.agent)
    assert any(p.startswith("the strategy file cannot be used: rebalance must be") for p in problems)
    with pytest.raises(golive.NotDone, match="The strategy file cannot be used"):
        go(s, "x", cfg=live)


def test_an_old_confirmation_from_version_0_1_is_refused_with_the_plain_explanation(s):
    live = s.write(FULL.replace('"paper"', '"live"'))
    old = (f"I approve live trading by Crypto Bot on mainnet for the account {MASTER.lower()} with the API wallet "
           f"{s.agent.lower()}, using at most 500 USDC.\n")
    live.confirm_file.write_text(old, encoding="utf-8")
    problems = keys_problems_with_key(live, s.agent)
    assert any(p.startswith("the confirmation you typed with an earlier version does not name the strategy") and
               "Real money stays off until you type the confirmation sentence again" in p for p in problems)
    s.preflight_exit = [0, 0]
    assert go(s, sentence(s), cfg=live) == sentence(s)  # go-live asks for the new sentence; not "already live"
    assert not any("confirm" in p for p in keys_problems_with_key(live, s.agent))
