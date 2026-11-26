"""The agent key and the live gate.

Where the key lives: a fresh agent private key is created on the server and stored ONLY there, encrypted at
rest with systemd-creds (the host key) in /etc/credstore.encrypted/, root-only. The bot's systemd units
import it with ImportCredential=, so systemd decrypts it into the service's private credentials
directory at start; the bot user cannot read the file itself. It is deliberately not copied anywhere else:
every place holding it could trade.

The key can only trade for the master account once its owner approves its ADDRESS on Hyperliquid. It cannot
withdraw: withdrawals are signed by the master key, which never comes near this bot.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import eth_account

from . import eligibility, strategy_file
from .config import ADDRESS, Config
from .names import DISPLAY_NAME, NAME


class LiveNotAllowed(RuntimeError):
    def __init__(self, problems):
        super().__init__("live trading is not allowed: " + "; ".join(problems))
        self.problems = problems


def credential_path(cfg: Config) -> Path:
    base = cfg.credentials_dir or os.environ.get("CREDENTIALS_DIRECTORY", "")
    return Path(base) / cfg.credential_name if base else Path()


def address_path(cfg: Config, network=None) -> Path:
    return Path(cfg.credstore_dir).parent / NAME / f"agent-address.{network or cfg.network}"


def load_agent_key(cfg: Config) -> str:
    p = credential_path(cfg)
    if not p.name or not p.is_file():
        raise LiveNotAllowed([f"no agent key credential {cfg.credential_name} (run keygen, then start the bot from "
                              f"its systemd unit or the {NAME} wrapper)"])
    key = p.read_text(encoding="utf-8").strip()
    if not key.startswith("0x") or len(key) != 66:
        raise LiveNotAllowed([f"credential {cfg.credential_name} is not a private key"])
    return key


def strategy_in_sentence(cfg: Config) -> str:
    """How the confirmation sentence names the strategy: a built-in by its name, a strategy file by its name and the
    file's fingerprint, so a changed file is a new strategy that needs a new sentence. Raises StrategyFileError."""
    if strategy_file.is_file_setting(cfg.strategy):
        spec = strategy_file.load_for(cfg)
        return f'your own strategy "{spec.name}" with the fingerprint {spec.fingerprint}'
    return f'the strategy "{cfg.strategy}"'


def _before_the_strategy(cfg: Config, agent_address: str) -> str:
    return (f"I approve live trading by {DISPLAY_NAME} on {cfg.network} for the account "
            f"{cfg.master_address.lower()} with the API wallet {agent_address.lower()}, using at most "
            f"{cfg.capital_cap_usdc:g} USDC")


def confirmation_text(cfg: Config, agent_address: str) -> str:
    """The exact sentence the confirmation file must hold: one line the person types themselves, naming the network,
    the account, the key, the cap and the strategy. The bot never writes this file."""
    return f"{_before_the_strategy(cfg, agent_address)}, running {strategy_in_sentence(cfg)}.\n"


NEW_SENTENCE = (f"Real money stays off until you type the confirmation sentence again: run {NAME} go-live in your own "
                "terminal on the server.")


def confirmation_problem(cfg: Config, agent_address: str, text: str):
    """Why a confirmation file does not open the gate, in plain words, or None when it matches."""
    text = text.replace(chr(13) + chr(10), chr(10))
    try:
        want = confirmation_text(cfg, agent_address)
    except strategy_file.StrategyFileError as exc:
        return f"the strategy file cannot be used: {exc.problems[0]}"
    if text == want:
        return None
    before = _before_the_strategy(cfg, agent_address)
    if text == before + ".\n":  # typed with version 0.1, before the sentence named the strategy
        return ("the confirmation you typed with an earlier version does not name the strategy, and the sentence now "
                "does. " + NEW_SENTENCE)
    rest = text[len(before):] if text.startswith(before) else ""
    if re.fullmatch(r', running (the strategy "[a-z]+"|your own strategy "[a-z][a-z0-9-]*" with the fingerprint '
                    r'[0-9a-f]{16})\.\n', rest):  # a well-formed sentence for another strategy
        return (f"the strategy in use ({strategy_in_sentence(cfg)}) is not the one you confirmed; changing the strategy "
                "or its file while trading real money needs a new sentence. " + NEW_SENTENCE)
    return (f"confirmation file {cfg.confirm_file} does not match this network, account, key, cap and strategy (only "
            f"`{NAME} go-live`, typed by the owner, writes it)")


def live_problems(cfg: Config, before_confirmation=False):
    """Everything standing between this configuration and live trading. Empty means allowed.

    ``before_confirmation`` leaves out only the confirmation file, so `go-live` can check everything else
    before it asks the person to type the sentence. Trading never calls it that way."""
    problems = []
    if cfg.mode != "live":
        problems.append(f"config mode is {cfg.mode!r}, not 'live'")
    if strategy_file.is_file_setting(cfg.strategy):
        try:
            strategy_file.load_for(cfg)
        except strategy_file.StrategyFileError as exc:
            problems.append(f"the strategy file cannot be used: {exc.problems[0]}")
    if not ADDRESS.match(cfg.master_address or ""):
        problems.append("master_address is not set")
    if not cfg.cap_named:
        problems.append("capital_cap_usdc is not set: name the most the bot may use (live has no default)")
    problems.extend(eligibility.problems(cfg))
    agent = None
    try:
        agent = eth_account.Account.from_key(load_agent_key(cfg)).address
    except LiveNotAllowed as exc:
        problems.extend(exc.problems)
    confirm = Path(cfg.confirm_file)
    if before_confirmation:
        return problems
    if not confirm.is_file():
        problems.append(f"confirmation file {confirm} does not exist (only the owner's own typed approval creates it)")
    elif agent and ADDRESS.match(cfg.master_address or ""):
        why = confirmation_problem(cfg, agent, confirm.read_text(encoding="utf-8"))
        if why and not why.startswith("the strategy file cannot be used"):  # that one is said above already
            problems.append(why)
    return problems


def systemd_creds_encrypt(name: str, secret: bytes, out: Path):
    subprocess.run(["systemd-creds", "encrypt", "--with-key=host", f"--name={name}", "-", str(out)],
                   input=secret, check=True, capture_output=True)


def systemd_creds_decrypt(name: str, path: Path) -> bytes:
    return subprocess.run(["systemd-creds", "decrypt", f"--name={name}", str(path), "-"], check=True,
                          capture_output=True).stdout


def keygen(cfg: Config, network: str, encrypt=systemd_creds_encrypt, decrypt=systemd_creds_decrypt,
           replace: bool = False, now=time.time) -> str:
    """Create a fresh agent key for ``network``, store it encrypted, and return ONLY its address.

    Refuses to replace an existing key unless ``replace`` is set (renewing an expiring key): a key whose address
    its owner approved must not silently change. A replaced key is kept aside, never deleted.
    """
    name = f"{cfg.credential_name.rsplit('.', 1)[0]}.{network}"
    store_dir = Path(cfg.credstore_dir)
    out = store_dir / name
    addr_file = address_path(cfg, network)
    if out.exists() and not replace:
        existing = addr_file.read_text(encoding="utf-8").strip() if addr_file.exists() else "unknown"
        raise FileExistsError(f"an agent key for {network} already exists (address {existing}); "
                              "to renew it, run keygen again with --replace")
    store_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(store_dir, 0o700)
    acct = eth_account.Account.create()
    secret = ("0x" + acct.key.hex().removeprefix("0x")).encode()
    # The working key is not touched until the new one is verified. Neither this file nor a replaced key may sit in
    # the credential store: a unit imports everything there by name and a stray file would stop it.
    fresh = addr_file.parent / "pending" / name
    fresh.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        encrypt(name, secret, fresh)
    finally:
        os.umask(old_umask)
    os.chmod(fresh, 0o600)
    if decrypt(name, fresh).strip() != secret:
        fresh.unlink()
        raise RuntimeError("the stored key does not decrypt to the generated key; nothing was kept")
    del secret
    if out.exists():
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(now()))
        kept = addr_file.parent / "replaced"
        kept.mkdir(parents=True, exist_ok=True)
        os.chmod(kept, 0o700)
        shutil.move(str(out), str(kept / f"{out.name}.{stamp}"))
        if addr_file.exists():
            shutil.move(str(addr_file), str(kept / f"{addr_file.name}.{stamp}"))
    shutil.move(str(fresh), str(out))
    addr_file.parent.mkdir(parents=True, exist_ok=True)
    addr_file.write_text(acct.address + chr(10), encoding="utf-8")
    os.chmod(addr_file, 0o644)
    return acct.address
