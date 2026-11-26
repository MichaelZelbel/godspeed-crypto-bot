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
import shutil
import subprocess
import time
from pathlib import Path

import eth_account

from . import eligibility
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


def confirmation_text(cfg: Config, agent_address: str) -> str:
    """The exact sentence the confirmation file must hold: one line the person types themselves. The bot never
    writes this file."""
    return (
        f"I approve live trading by {DISPLAY_NAME} on {cfg.network} for the account "
        f"{cfg.master_address.lower()} with the API wallet {agent_address.lower()}, using at most "
        f"{cfg.capital_cap_usdc:g} USDC.\n"
    )


def live_problems(cfg: Config, before_confirmation=False):
    """Everything standing between this configuration and live trading. Empty means allowed.

    ``before_confirmation`` leaves out only the confirmation file, so `go-live` can check everything else
    before it asks the person to type the sentence. Trading never calls it that way."""
    problems = []
    if cfg.mode != "live":
        problems.append(f"config mode is {cfg.mode!r}, not 'live'")
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
        if confirm.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != confirmation_text(cfg, agent):
            problems.append(f"confirmation file {confirm} does not match this network, master, agent and cap "
                            f"(only `{NAME} go-live`, typed by the owner, writes it)")
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
