"""`godspeed-crypto-bot uninstall`: take the add-on off this server, and say plainly what stays.

Stops and removes the timers and units, the command, the page's address in Godspeed, the program and the
system user. State and configuration stay unless `--purge`. It never touches the exchange: positions stay open
there (run `flatten --yes` first to close them), and the API wallet stays approved on Hyperliquid until its
owner removes it on Hyperliquid's API page. In live mode it refuses while the account holds positions the
bot opened, unless `--yes`.
"""

from __future__ import annotations

import dataclasses
import glob
import json
import shutil
from pathlib import Path

from . import chat_door, config, godspeed
from .names import CODE_DIR, CONFIG_DIR, DISPLAY_NAME, NAME, PAGE_DIR, STATE_DIR

UNIT_KINDS = ("daily", "guard", "deadline", "report", "terms", "relay")
SYSTEMD_DIR = "/etc/systemd/system"
WRAPPER = f"/usr/local/sbin/{NAME}"
CREDSTORE = "/etc/credstore.encrypted"


class Refused(Exception):
    """A plain sentence for the owner; nothing was changed."""


def live_positions(cfg, market=None):
    """The positions an uninstall would leave open, {"perp:BTC": size}, whatever mode and network the configuration
    says now: every network is looked at when the account is named and real trading ever had a reason to exist on
    it (the mode is live there, or a confirmation file or a key is there). Raises when it cannot find out."""
    from . import keys

    held = {}
    for network in config.NETWORKS:
        c = dataclasses.replace(cfg, network=network)
        ever_live = (cfg.mode == "live" and cfg.network == network or Path(c.confirm_file).exists()
                     or keys.address_path(c).exists())
        if not (cfg.master_address or Path(cfg.confirm_dir, f"config.{network}.toml").is_file()) or not ever_live:
            continue
        # Each network may have a configuration of its own (its own account and tokens): that one is the one asked.
        own = Path(cfg.confirm_dir) / f"config.{network}.toml"
        track = config.load(own) if network != cfg.network and own.is_file() else c
        m = market
        if m is None:
            from .venue.hyperliquid import HyperliquidMarketData

            m = HyperliquidMarketData(network, track.spot_tokens)
        account = track.master_address or cfg.master_address
        for key, size in m.open_holdings(account).items():
            held[f"{key} ({network})"] = size
    return held


def check(cfg, yes=False, market=None):
    """Refuse (raise Refused) while the account holds positions, unless `yes`."""
    try:
        held = live_positions(cfg, market)
    except Exception as exc:  # noqa: BLE001
        if yes:
            return None
        raise Refused(f"{DISPLAY_NAME} could not check Hyperliquid for open positions ({type(exc).__name__}); "
                      "run it again, or add --yes if you know none are open") from exc
    if held and not yes:
        listing = ", ".join(f"{k.split(':')[1]} {v:g}" for k, v in sorted(held.items()))
        raise Refused(f"The account still holds positions {DISPLAY_NAME} opened ({listing}); they stay open if you "
                      f"remove it. Run `{NAME} flatten --yes` first to close them, or add --yes to remove it anyway")
    return held


def docker_assistant_root(cfg, gs, run=godspeed.sh, root="/"):
    """Where the assistant's folder is on the one-line Docker server: asked of the container, or, when Godspeed is not
    running, where the relay last found it."""
    try:
        found = godspeed.assistant_root(gs, run)
    except Exception:  # noqa: BLE001 - the remembered place below is tried instead
        found = None
    if found:
        return found
    try:
        doc = json.loads((Path(root) / str(cfg.relay_path).lstrip("/") / godspeed.CHAT_ROOT_FILE).read_text(
            encoding="utf-8"))
        return Path(doc["base"]), [str(p) for p in doc["parts"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remove_recipe(cfg, gs=None, run=godspeed.sh, root="/"):
    """The recipe the installer put where Godspeed's assistant reads recipes (only a folder it marked): in the Godspeed
    folder on a native install, in the assistant's folder inside the container's volume on the one-line Docker server.
    The person's crypto-bot folder with their strategy files stays."""
    if not cfg.godspeed_dir:
        return
    for room in ("skills", ".claude/skills"):
        recipe = Path(cfg.godspeed_dir) / room / "crypto-bot"
        if (recipe / f".installed-by-{NAME}").is_file():
            shutil.rmtree(recipe, ignore_errors=True)
    found = docker_assistant_root(cfg, gs, run, root) if gs is not None and gs.docker else None
    if not found:
        return
    base, parts = found
    for room in (["skills"], [".claude", "skills"]):
        try:  # walked as the installer walked it: never through a link, only folders of the folder's owner
            d = chat_door.open_chain(base, [*parts, *room])
        except (OSError, chat_door.Refused):
            continue
        try:
            if d.exists(chat_door.RECIPE) and chat_door._marked(d, chat_door.RECIPE):
                d.remove_tree(chat_door.RECIPE)
        except (OSError, chat_door.Refused):
            pass
        finally:
            d.close()


def unit_files(root="/"):
    base = Path(root) / SYSTEMD_DIR.lstrip("/")
    return [base / f"{NAME}-{k}.{ext}" for k in UNIT_KINDS for ext in ("timer", "service")]


def uninstall(cfg, purge=False, yes=False, run=godspeed.sh, root="/", market=None, say=print):
    """Returns the lines that tell the owner what was removed and what stays."""
    check(cfg, yes=yes, market=market)
    at = lambda p: Path(root) / str(p).lstrip("/")  # noqa: E731
    gs = godspeed.find(cfg.godspeed_dir) if cfg.godspeed_dir else None
    # Stop everything first, and stop altogether if something will not stop: a bot that keeps trading after
    # "removed" is the one outcome that must not happen.
    stubborn, timers_off = [], []
    for k in UNIT_KINDS:
        for unit, argv in ((f"{NAME}-{k}.timer", ["disable", "--now"]), (f"{NAME}-{k}.service", ["stop"])):
            if (at(SYSTEMD_DIR) / unit).exists():
                if run(["systemctl", *argv, unit])[0] != 0:
                    stubborn.append(unit)
                elif unit.endswith(".timer"):
                    timers_off.append(unit)
    if stubborn:
        for unit in timers_off:  # a refusal changes nothing: what was switched off is switched on again
            run(["systemctl", "enable", "--now", unit])
        raise Refused(f"{', '.join(stubborn)} would not stop, so nothing was removed; see: systemctl status {stubborn[0]}")
    for f in unit_files(root):
        f.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "reset-failed"])
    remove_recipe(cfg, gs, run, root)
    godspeed.remove_page(gs, run, root)  # the page's address in Godspeed's Caddy, or the add-on's own container
    at(WRAPPER).unlink(missing_ok=True)
    # Godspeed's door lets an add-on make and move its reminder, not close it: the owner closes it there.
    reminder = (at(cfg.relay_path) / godspeed.DEADLINE_SYNCED).is_file()
    kept = []
    if purge:
        for d in (STATE_DIR, f"{STATE_DIR}-relay", PAGE_DIR, f"{PAGE_DIR}-front", CONFIG_DIR):
            shutil.rmtree(at(d), ignore_errors=True)
        for f in glob.glob(str(at(CREDSTORE) / f"{NAME}.agent-key.*")):
            Path(f).unlink(missing_ok=True)
    else:
        kept = [f"the records and the stopped bot's state ({STATE_DIR})", f"the configuration ({CONFIG_DIR})",
                "the bot's encrypted key"]
    leftovers = []
    if run(["id", NAME])[0] == 0:
        for argv in (["userdel", NAME], ["groupdel", NAME]):
            code, _out, err = run(argv)
            if code != 0 and run(["getent", "passwd", NAME])[0] == 0:
                leftovers.append(f"{argv[0]} said: {(err or '').strip()[:120]}")
    shutil.rmtree(at(CODE_DIR), ignore_errors=True)  # last: this program runs from there
    lines = [f"{DISPLAY_NAME} is removed from this server: its timers, command, page address, program and user."]
    if kept:
        lines.append("Kept: " + "; ".join(kept) + f". Remove them too with `{NAME} uninstall --purge`, "
                     "or run the install line again to pick up where it was.")
    else:
        lines.append("Purged: its records, configuration and encrypted key are gone too.")
    lines.append("Nothing changed at Hyperliquid: positions stay open unless `flatten --yes` ran first, and the API "
                 "wallet stays approved there until you remove it on Hyperliquid's API page.")
    if reminder:
        lines.append(f"The reminder to renew {DISPLAY_NAME}'s exchange key stays among your Godspeed reminders; close "
                     "it there if you no longer need it.")
    if leftovers:
        lines.insert(0, f"The system user {NAME} could not be removed ({'; '.join(leftovers)}); remove it by hand.")
    for line in lines:
        say(line)
    if leftovers:
        raise Refused("the removal is not complete (see above)")
    return lines
