"""`godspeed-crypto-bot go-live`: the one way real money gets switched on, and only by the person typing.

It checks everything that can be checked first (nothing is written if anything fails), prints the exact
confirmation sentence (network, account, API wallet, the most the bot may use), reads ONE line typed on the
terminal, and only an exact match writes the confirmation file. Standard input that is not a terminal is
refused. After the file is written the bot's own preflight runs once more in live mode; only when that is clean
does the configuration change to mode = "live". The bot itself never writes the confirmation file, and the
trading gate (keys.live_problems) is the same one every trading command asks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import configure, eligibility, godspeed, keys
from .config import ADDRESS
from .names import DISPLAY_NAME, NAME

WRAPPER = f"/usr/local/sbin/{NAME}"


class NotDone(Exception):
    """A plain sentence for the person; nothing was written."""


def _preflight(run, cfg, config_path, *args):
    code, out, err = run([WRAPPER, "--config", str(config_path), "--mode", "live", "preflight", *args], timeout=300)
    return code, (out or "") + (err or "")


def go_live(cfg, config_path, stdin=None, stdout=None, ask=input, run=godspeed.sh, say=print) -> str:
    """Returns the sentence the person typed. Raises NotDone with the reason otherwise."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not (stdin.isatty() and stdout.isatty()):
        raise NotDone("go-live must be run by you, typing in a terminal. It reads your own typed confirmation, "
                      "so it refuses input that is not a terminal, and nothing was changed.")
    agent_now = keys.address_path(cfg)
    if (cfg.mode == "live" and Path(cfg.confirm_file).is_file() and agent_now.is_file()
            and Path(cfg.confirm_file).read_text(encoding="utf-8") == keys.confirmation_text(
                cfg, agent_now.read_text(encoding="utf-8").strip())):
        raise NotDone(f"{DISPLAY_NAME} is already set to trade live on {cfg.network}.")  # a renewed key, a new cap: not
    if not ADDRESS.match(cfg.master_address or ""):
        raise NotDone("master_address is not set: it is your Hyperliquid account address "
                      f"(run: {NAME} configure --master <address>).")
    if not cfg.cap_named:
        raise NotDone("You have not named the most the bot may use: live trading has no default "
                      f"(run: {NAME} configure --cap <USDC>).")
    problems = eligibility.problems(cfg)
    if problems:
        raise NotDone(problems[0])
    agent_file = keys.address_path(cfg)
    if not agent_file.is_file():
        raise NotDone(f"There is no key for {cfg.network} yet (run: {NAME} keygen --network {cfg.network}, "
                      "approve its address on Hyperliquid, then run go-live again).")
    agent = agent_file.read_text(encoding="utf-8").strip()

    code, said = _preflight(run, cfg, config_path, "--before-confirmation")
    if code != 0:
        raise NotDone("The bot's own check is not clean yet, so nothing was changed:" + chr(10) + said.strip())

    sentence = keys.confirmation_text(cfg, agent).strip()
    say("To switch on real money, type this whole sentence, exactly as it is, and press Enter:")
    say("")
    say(sentence)
    say("")
    typed = ask("> ")
    if typed.rstrip(chr(13) + chr(10)) != sentence:  # the sentence exactly; only the Enter is dropped
        raise NotDone("That is not the same sentence, so nothing was changed.")

    # The pre-check above was clean and the person has typed the sentence; the file is written, the bot's own check
    # runs once more with it, and only then does the configuration say live. If that last check fails, what was
    # there before (nothing, or the confirmation of an earlier key) is put back.
    confirm = Path(cfg.confirm_file)
    confirm.parent.mkdir(parents=True, exist_ok=True)
    before = confirm.read_text(encoding="utf-8") if confirm.is_file() else None
    tmp = confirm.with_name(confirm.name + f".{os.getpid()}.tmp")
    tmp.write_text(sentence + chr(10), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, confirm)
    code, said = _preflight(run, cfg, config_path)
    if code != 0:
        if before is None:
            confirm.unlink(missing_ok=True)
        else:
            confirm.write_text(before, encoding="utf-8")
        raise NotDone("The bot's final check failed, so the confirmation was taken back and nothing is live:"
                      + chr(10) + said.strip())
    configure.set_value(config_path, "mode", "live")
    return sentence
