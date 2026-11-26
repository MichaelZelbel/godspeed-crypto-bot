"""godspeed-crypto-bot: the commands of Crypto Bot.

Scheduled (systemd): run-daily 00:02 UTC, guard every 5 minutes, check-deadline 00:45 UTC, report weekly,
terms-check weekly. By hand: status, flatten, halt, resume, keygen, preflight, go-live, configure, account-mode,
terms-accept, export, page, test-alert, strategy check, backtest, chat-door, and on testnet only selftest. run-daily, flatten, halt and resume refresh the owner's
page afterwards, and so does a guard run that traded, moved money or alerted (page.py).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import (
    backtest,
    bot,
    chat_door,
    config,
    configure,
    eligibility,
    export,
    godspeed,
    golive,
    keys,
    names,
    outbox,
    page,
    report,
    safety,
    selftest,
    strategy_file,
    terms,
    uninstall,
)
from .data import DataNotReady
from .notify import Notifier
from .venue.hyperliquid import HyperliquidMarketData

MAIN_START = time.time()
EARLY_S = 5

EXIT_TEMPORARY = 75  # data not ready yet: systemd retries
EXIT_CONFIG = 78  # live is configured but not allowed: systemd must not retry (RestartPreventExitStatus=78)


def _print(obj):
    print(json.dumps(obj, indent=1, default=str))


def _proc_start():
    """When this process was created (before Python imported anything), from /proc; None where unknown."""
    try:
        with open("/proc/self/stat", encoding="ascii") as fh:
            start_ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat", encoding="ascii") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def _schedule(started):
    """The slot a timer-started run belongs to: the units set GODSPEED_CRYPTO_BOT_EVERY/_OFFSET (seconds)."""
    try:
        every = int(os.environ.get("GODSPEED_CRYPTO_BOT_EVERY", "0"))
        offset = int(os.environ.get("GODSPEED_CRYPTO_BOT_OFFSET", "0"))
    except ValueError:
        return None, None
    if every <= 0:
        return None, None
    # /proc's process start is good to about a second (boot time is whole seconds), and a timer may fire a hair
    # early: a start up to EARLY_S before a slot belongs to that slot.
    return every, ((started - offset + EARLY_S) // every) * every + offset


def _compact(cmd, result):
    """What an invocation record keeps of a command's result."""
    if not isinstance(result, dict):
        return result
    if cmd == "run-daily":
        keep = ("day", "status", "note", "strategy_equity", "carry_active", "restrictions", "peg_gaps", "warnings")
        out = {k: result.get(k) for k in keep if k in result}
        out["orders"] = len(result.get("orders", []))
        out["pair_failures"] = len(result.get("pair_failures", []))
        return out
    if cmd == "selftest":
        return {"ok": result.get("ok"), "session": result.get("session"),
                "steps": {s["step"]: s["ok"] for s in result.get("steps", [])}}
    return result


REFRESHES = ("run-daily", "flatten", "halt", "resume")


def _activity(cfg, store):
    """What a guard run can change that the page shows: its actions, orders, alerts, and the HALT file."""
    row = store.db.execute(
        "SELECT (SELECT MAX(id) FROM guard_actions WHERE mode=? AND network=?), "
        "(SELECT MAX(id) FROM orders WHERE mode=? AND network=?), "
        "(SELECT MAX(id) FROM alerts WHERE mode=? AND network=?)",
        (cfg.mode, cfg.network) * 3).fetchone()
    return tuple(row) + (cfg.halt_path.exists(),)


def _context(args):
    cfg = config.load(args.config, mode=args.mode, network=args.network, state_dir=args.state_dir)
    store = bot.open_store(cfg)
    try:  # what came through the chat door, from the relay's log into the bot's own records
        chat_door.import_log(cfg, store)
    except Exception as exc:  # noqa: BLE001 - never a reason for a command not to run
        logging.getLogger(__name__).warning("chat requests not copied into the records: %s", exc)
    return cfg, store, Notifier(cfg, store)


def _report_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value or ""):
        raise argparse.ArgumentTypeError("a report name is letters, digits, dashes and underscores")
    return value


def _chat_door_setup(cfg, version):
    """root, from the installer: the assistant's crypto-bot folder and its recipe, on a native or a Docker Godspeed."""
    gs = godspeed.find(cfg.godspeed_dir)
    if gs is None:
        print(f"{names.NAME}: godspeed_dir in the configuration does not name a Godspeed folder "
              f"({cfg.godspeed_dir or 'not set'})", file=sys.stderr)
        return EXIT_CONFIG
    root = godspeed.assistant_root(gs)
    if root is None:
        print(f"{names.NAME}: Godspeed's assistant's folder was not found (is Godspeed running?); the chat door is not "
              "set up, everything else works.", file=sys.stderr)
        return 1
    old = [str(Path(gs.root) / "skills" / "crypto-bot"), str(Path(gs.root) / ".claude" / "skills" / "crypto-bot")] \
        if gs.docker else []
    try:
        said = chat_door.setup(root, Path(__file__).resolve().parent.parent, version, old_places=old)
    except (chat_door.Refused, OSError) as exc:
        print(f"{names.NAME}: the chat door could not be set up: {exc}", file=sys.stderr)
        return 1
    for line in said:
        print(line)
    return 0


def _strategy_choice(value):
    """argparse type for --strategy: a built-in or file:<name>."""
    try:
        return strategy_file.check_setting(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _listed_perps(cfg):
    """The perpetuals Hyperliquid lists now, for checking a strategy file's coins (the tests replace this)."""
    return HyperliquidMarketData(cfg.network).listed_perps()


def _check_file(cfg, path):
    """(spec, problems) for a strategy file, its coins checked against what Hyperliquid lists now."""
    try:
        perps = _listed_perps(cfg)
    except Exception as exc:  # noqa: BLE001 - the coins cannot be checked: nothing is accepted
        return None, [f"Could not reach Hyperliquid to check that the coins exist ({type(exc).__name__}); nothing "
                      "was checked. Try again in a minute."]
    try:
        return strategy_file.load(path, perps=perps), []
    except strategy_file.StrategyFileError as exc:
        return None, exc.problems


def _readable_by_the_bot(path):
    """The scheduled runs read the file as the bot's unprivileged user: it and its folder must be readable by others."""
    try:
        return bool(os.stat(path).st_mode & 0o004) and bool(os.stat(Path(path).parent).st_mode & 0o001)
    except OSError:
        return False


def _strategy(args, cfg):
    spec, problems = _check_file(cfg, args.file)
    if args.json:
        _print({"ok": spec is not None, "problems": problems,
                **({"name": spec.name, "setting": spec.setting, "fingerprint": spec.fingerprint,
                    "summary": strategy_file.summary(spec), "explanation": strategy_file.explain(spec)}
                   if spec else {})})
        return 0 if spec else 1
    if spec is None:
        print("The bot cannot use this strategy file:")
        for line in problems:
            print(f"- {line}")
        return 1
    print(f'Strategy "{spec.name}", as the bot understood it:')
    print(strategy_file.explain(spec))
    print(f"Fingerprint: {spec.fingerprint} (it changes whenever the file changes).")
    target = strategy_file.path_for(cfg.strategies_dir, spec.name)
    print(f"To run it on paper: put it at {target} and run {names.NAME} configure --strategy {spec.setting}")
    return 0


def _relay(cfg, config_path):
    gs = godspeed.find(cfg.godspeed_dir)
    if gs is None:
        print(f"{names.NAME}: godspeed_dir in the configuration does not name a Godspeed folder "
              f"({cfg.godspeed_dir or 'not set'})", file=sys.stderr)
        return EXIT_CONFIG
    _print(godspeed.Relay(cfg, gs, config_path=config_path)())  # only the main configuration answers the chat door
    practice = page.TESTNET_CONFIG  # a second track keeps its own outbox and relay folder
    if practice.exists():
        c = config.load(practice)
        if c.state_dir != cfg.state_dir:
            c.godspeed_dir = c.godspeed_dir or cfg.godspeed_dir
            _print(godspeed.Relay(c, gs)())
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog=names.NAME, description=__doc__.splitlines()[0])
    ap.add_argument("--config", help=f"TOML file (default {config.DEFAULT_CONFIG})")
    ap.add_argument("--mode", choices=config.MODES, help="override the configured mode")
    ap.add_argument("--network", choices=config.NETWORKS, help="override the configured network")
    ap.add_argument("--state-dir", help="override the state directory")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run-daily", help="the 00:02 UTC decision and its orders")
    p.add_argument("--allow-late", action="store_true", help="run even after the morning cut-off (manual runs)")
    sub.add_parser("guard", help="the five-minute check: holdings, Unit peg, drawdown, margin")
    sub.add_parser("check-deadline", help="alert if today's run has not finished; API wallet expiry check")
    sub.add_parser("status", help="what the bot is doing, from its records")
    sub.add_parser("test-alert", help="queue one test message for your Godspeed's Telegram, to check that it arrives")
    p = sub.add_parser("uninstall", help=f"root: remove {names.DISPLAY_NAME} from this server (keeps records and settings unless --purge)")
    p.add_argument("--purge", action="store_true", help="also remove the records, settings and the encrypted key")
    p.add_argument("--yes", action="store_true", help="remove even though the account may still hold open positions")
    sub.add_parser("relay", help="root: tell the owner through Godspeed, keep the key reminder and the page's address")
    p = sub.add_parser("report", help="the comparison with the backtest's assumptions")
    p.add_argument("--weekly", action="store_true", help="write the weekly report file")
    p = sub.add_parser("flatten", help="close every position and halt")
    p.add_argument("--yes", action="store_true", help="required: this sends closing orders in live mode")
    p.add_argument("--reason", default="by hand")
    p = sub.add_parser("halt", help="stop all trading (writes the HALT file)")
    p.add_argument("--reason", default="by hand")
    sub.add_parser("resume", help="remove the HALT file")
    p = sub.add_parser("keygen", help="create the agent key on this server; prints only its address")
    p.add_argument("--network", dest="key_network", choices=config.NETWORKS, required=True)
    p.add_argument("--replace", action="store_true", help="renew: keep the old key aside and make a new one")
    p = sub.add_parser("preflight", help="everything that stands between this configuration and trading")
    p.add_argument("--before-confirmation", action="store_true",
                   help="leave out only the confirmation file (what go-live checks first); never used by trading")
    sub.add_parser("go-live", help="you: type the confirmation sentence that switches on real money")
    p = sub.add_parser("configure", help="root: set the account address, the most the bot may use, the strategy")
    p.add_argument("--master", help="your Hyperliquid account address (the wallet address is the account)")
    p.add_argument("--cap", type=float, help="the most the bot may use, in USDC (live has no default)")
    p.add_argument("--strategy", type=_strategy_choice, metavar="mix|trend|carry|file:<name>",
                   help="a built-in strategy, or your own strategy file in the strategies folder")
    p.add_argument("--network", dest="set_network", choices=config.NETWORKS)
    p.add_argument("--agent-approved-on", dest="approved_on", metavar="YYYY-MM-DD",
                   help="the day you approved the bot's API wallet on Hyperliquid, written YYYY-MM-DD")
    p = sub.add_parser("strategy", help="your own strategy: check a strategy file and say what the bot understood")
    p.add_argument("action", choices=("check",))
    p.add_argument("file", help="the strategy file (TOML)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p = sub.add_parser("backtest", help="test a strategy against Hyperliquid's past prices (public data only, no key)")
    p.add_argument("strategy", metavar="mix|trend|carry|file:<name>")
    p.add_argument("--start", type=date.fromisoformat, help="first day to test (default: the earliest possible)")
    p.add_argument("--end", type=date.fromisoformat, help="last day to test (default: yesterday, the last closed day)")
    p.add_argument("--json", action="store_true", help="print the JSON report instead of the Markdown one")
    p.add_argument("--out-name", type=_report_name, help=argparse.SUPPRESS)  # the chat door's own name for the report
    p = sub.add_parser("chat-door", help="root: make the assistant's crypto-bot folder and put its recipe in place")
    p.add_argument("--version", default="", help="the release being installed (kept with the recipe)")
    p = sub.add_parser("eligibility", help="root: record the answer to the country question")
    p.add_argument("--restricted", choices=("yes", "no"), required=True,
                   help="yes if you live in the US or Ontario, or are a resident or citizen of a sanctioned country")
    sub.add_parser("account-mode", help="show the account's balance mode (unifiedAccount or disabled)")
    sub.add_parser("terms-check", help="the weekly Hyperliquid terms check")
    sub.add_parser("terms-accept", help="adopt changed terms after review and lift the block on new positions")
    p = sub.add_parser("export", help="tax-ready CSV of fills, fees, funding and transfers (UTC, EUR blank)")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--out", help="file to write (default: stdout)")
    p = sub.add_parser("page", help="redraw the page (the account, its trades, the practice exchange)")
    p.add_argument("--out-dir", help=f"where to write {page.PAGE_NAME} (default {page.PAGE_DIR})")
    p.add_argument("--practice-config", help=f"the practice exchange's config (default {page.TESTNET_CONFIG})")
    p = sub.add_parser("selftest", help="testnet only: prove transfers, batches, the agent's limits, the records")
    p.add_argument("--spot", help="spot token or pair to trade in step 3 (default: the first with a usable book)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        if args.command == "keygen":
            cfg = config.load(args.config)
            address = keys.keygen(cfg, args.key_network, replace=args.replace)
            print(address)
            return 0
        if args.command == "relay":  # root: never opens the bot's records, which belong to the bot's user
            return _relay(config.load(args.config, mode=args.mode, network=args.network, state_dir=args.state_dir),
                          config.config_path(args.config))
        if args.command == "go-live":
            cfg = config.load(args.config, mode=args.mode, network=args.network, state_dir=args.state_dir)
            try:
                golive.go_live(cfg, config.config_path(args.config))
            except golive.NotDone as exc:
                print(f"{names.NAME}: {exc}", file=sys.stderr)
                return 1
            print(f"Done. {cfg.network} is live: from the next daily run (00:02 UTC) real orders are placed, "
                  f"never more than {cfg.capital_cap_usdc:g} USDC of yours. Stop it any time with: "
                  f"{names.NAME} halt")
            return 0
        if args.command == "backtest":  # public data only: no records, no venue, no key
            cfg = config.load(args.config, mode=args.mode, network=args.network, state_dir=args.state_dir)
            try:
                md, js, text = backtest.run(cfg, args.strategy, args.start, args.end, name=args.out_name)
            except backtest.BacktestError as exc:
                print(f"{names.NAME}: {exc}", file=sys.stderr)
                return 1
            sys.stdout.write(js.read_text(encoding="utf-8") if args.json else text)
            print(f"written: {md} and {js}", file=sys.stderr)
            return 0
        if args.command == "strategy":
            return _strategy(args, config.load(args.config, network=args.network))
        if args.command == "chat-door":
            return _chat_door_setup(config.load(args.config), args.version)
        if args.command == "configure":
            path = config.config_path(args.config)
            now = config.load(path)
            wanted = args.strategy or now.strategy
            if strategy_file.is_file_setting(wanted) and (args.strategy or args.set_network):
                # the file is checked for the configuration this command makes: its strategy on its network
                target = strategy_file.path_for(now.strategies_dir, strategy_file.setting_name(wanted))
                _spec, problems = _check_file(dataclasses.replace(now, network=args.set_network or now.network),
                                              target)
                if not problems and not _readable_by_the_bot(target):
                    problems = [f"The bot reads strategy files as its own user, so {target.name} and its folder must "
                                f"be readable by everyone on this server (chmod 644 {target})."]
                if problems:
                    print(f"{names.NAME}: the strategy file {target} cannot be used, so nothing was changed:",
                          file=sys.stderr)
                    for line in problems:
                        print(f"- {line}", file=sys.stderr)
                    return 1
            for key, value in (("master_address", args.master), ("capital_cap_usdc", args.cap),
                               ("strategy", args.strategy), ("network", args.set_network),
                               ("agent_approved_on", args.approved_on)):
                if value is not None:
                    configure.set_value(path, key, value)
            cfg = config.load(path)
            _print({"network": cfg.network, "strategy": cfg.strategy,
                    "strategy_in_words": strategy_file.words(cfg), "master_address": cfg.master_address,
                    "capital_cap_usdc": cfg.capital_cap_usdc if cfg.cap_named else None, "mode": cfg.mode})
            if cfg.mode == "live" and args.strategy and args.strategy != now.strategy:
                print(f"The strategy changed while {names.DISPLAY_NAME} trades real money, and the confirmation you "
                      f"typed names the strategy. {keys.NEW_SENTENCE}", file=sys.stderr)
            return 0
        if args.command == "eligibility":
            cfg = config.load(args.config, mode=args.mode, network=args.network, state_dir=args.state_dir)
            eligibility.record(cfg, args.restricted == "yes")
            print(eligibility.REFUSAL + " Live trading is refused from now on." if args.restricted == "yes"
                  else "Recorded: you are not in a restricted place.")
            return 0
        if args.command == "uninstall":
            cfg = config.load(args.config, mode=args.mode, network=args.network, state_dir=args.state_dir)
            try:
                uninstall.uninstall(cfg, purge=args.purge, yes=args.yes)
            except uninstall.Refused as exc:
                print(f"{names.NAME}: {exc}", file=sys.stderr)
                return 1
            return 0
        cfg, store, notifier = _context(args)
    except ValueError as exc:  # a setting the bot refuses: one plain sentence, and systemd must not retry
        print(f"{names.NAME}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    proc_start = _proc_start()
    every, slot = _schedule(proc_start or MAIN_START)
    inv = store.start_invocation(args.command, os.environ.get("GODSPEED_CRYPTO_BOT_UNIT") or "by hand", every, slot,
                                 proc_start, MAIN_START, os.getpid())
    rec = {"outcome": None}
    code, error = None, None
    before = _activity(cfg, store) if args.command == "guard" else None
    try:
        code = _dispatch(args, cfg, store, notifier, rec)
        if args.command in REFRESHES or (before is not None and _activity(cfg, store) != before):
            page.auto_refresh()
        return code
    except BaseException as exc:
        code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            store.finish_invocation(inv, code, _compact(args.command, rec["outcome"]), error,
                                    list(HyperliquidMarketData.RETRIES))
        finally:
            store.close()


def _dispatch(args, cfg, store, notifier, rec=None):
    rec = rec if rec is not None else {}
    cmd = args.command
    if cmd == "status":
        _print(bot.status(cfg, store))
        return 0
    if cmd == "test-alert":
        # Marked as a test in every mode (also live on mainnet), and not recorded as an alert: nothing happened.
        text = (f"{names.DISPLAY_NAME} (test message): this is how it tells you when something needs you. "
                "Nothing happened to the account; there is nothing to do.")
        if not outbox.put_alert(cfg, f"test-alert:{time.time_ns()}", text):
            print(f"{names.NAME}: the test message could not be written to the outbox", file=sys.stderr)
            return 1
        relay = godspeed.read_status(cfg) or {}
        print("The test message is queued for your Godspeed's Telegram. Check whether it arrives." if relay.get("channel") is not False else
              f"The test message is queued, but {godspeed.NO_CHANNEL}.")
        return 0
    if cmd == "report":
        if args.weekly:
            path, text = report.weekly(cfg, store)
            print(text)
            print(f"\nwritten: {path}", file=sys.stderr)
        else:
            _print(report.compare(store))
        return 0
    if cmd == "halt":
        bot.write_halt(cfg, args.reason)
        _print({"halted": True, "file": str(cfg.halt_path)})
        return 0
    if cmd == "resume":
        if cfg.halt_path.exists():
            cfg.halt_path.unlink()
        _print({"halted": False})
        return 0
    if cmd == "terms-check":
        rec["outcome"] = terms.check_terms(store, notifier)
        _print(rec["outcome"])
        return 0
    if cmd == "page":
        # The other track comes from its standard config when it exists (page.tracks).
        main_cfg = cfg if cfg.network == "mainnet" else None
        practice_cfg = config.load(args.practice_config) if args.practice_config else (
            cfg if cfg.network == "testnet" else None)
        path = page.refresh(main_cfg, practice_cfg, out_dir=args.out_dir)
        rec["outcome"] = {"page": str(path)}
        _print({"page": str(path), "url": page.url(cfg)})
        return 0
    if cmd == "terms-accept":
        _print({"accepted": terms.accept_terms(store)})
        return 0
    if cmd == "export":
        if args.out:
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                _, n = export.to_csv(store, args.start, args.end, fh)
            print(f"{n} rows written to {args.out}", file=sys.stderr)
        else:
            text, n = export.to_csv(store, args.start, args.end)
            sys.stdout.write(text)
        return 0
    if cmd == "check-deadline":
        venue = None
        if cfg.mode == "live":
            try:
                venue = bot.build_venue(cfg, store)
            except keys.LiveNotAllowed as exc:
                notifier.alert("live-gate", "is set to live but may not trade", "; ".join(exc.problems),
                               f"Ask your assistant to check {names.DISPLAY_NAME}'s live setup.")
        out = bot.check_deadline(cfg, store, notifier, venue=venue)
        rec["outcome"] = out
        _print(out)
        return 0 if out.get("ok") else 1
    if cmd == "preflight":
        problems = keys.live_problems(cfg, before_confirmation=args.before_confirmation) if cfg.mode == "live" else []
        venue_problems, venue = [], None
        if not problems:
            try:
                venue = bot.build_venue(cfg, store, before_confirmation=args.before_confirmation)
                venue_problems = venue.preflight()
                venue_problems += safety.expiry_problems(cfg, store, venue, datetime.now(timezone.utc))
            except keys.LiveNotAllowed as exc:
                venue_problems = exc.problems
        out = {"mode": cfg.mode, "network": cfg.network, "ready": not (problems or venue_problems),
               "problems": problems + venue_problems}
        if not problems and getattr(venue, "preflight_notes", None):
            out["notes"] = venue.preflight_notes
        _print(out)
        return 0 if not (problems or venue_problems) else 1
    if cmd == "selftest":
        why = selftest.refusal(cfg)  # before any key is loaded: never on mainnet
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return 2
    try:
        venue = bot.build_venue(cfg, store)
    except keys.LiveNotAllowed as exc:
        notifier.alert("live-gate", "is set to live but may not trade", "; ".join(exc.problems),
                       f"Ask your assistant to check {names.DISPLAY_NAME}'s live setup.")
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if cmd == "account-mode":
        _print({"mode": venue.mode() if venue.signs else "simulated"})
        return 0
    if cmd == "selftest":
        try:
            out = selftest.run(cfg, venue, store, spot_choice=args.spot)
        except selftest.SelftestRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        rec["outcome"] = out
        _print(out)
        return 0 if out["ok"] else 1
    if cmd == "run-daily":
        try:
            out = bot.run_daily(cfg, venue, store, notifier, allow_late=args.allow_late)
        except strategy_file.StrategyFileError as exc:  # systemd must not retry: the file needs fixing first
            rec["outcome"] = {"status": "strategy file refused", "note": str(exc)}
            print(f"{names.NAME}: the strategy file cannot be used: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        except DataNotReady as exc:
            rec["outcome"] = {"status": "data not ready", "note": str(exc)}
            print(f"data not ready: {exc}", file=sys.stderr)
            return EXIT_TEMPORARY
        rec["outcome"] = out
        _print(out)
        return 0
    if cmd == "guard":
        out = bot.guard(cfg, venue, store, notifier)
        rec["outcome"] = out
        _print(out)
        return 0
    if cmd == "flatten":
        if not args.yes:
            print("flatten closes every position; pass --yes", file=sys.stderr)
            return 2
        with bot.trading_lock(cfg) as got:
            if not got:
                print("another trading process holds the lock; try again", file=sys.stderr)
                return 1
            residual = bot.flatten(cfg, venue, store, notifier, args.reason, datetime.now(timezone.utc).date())
        rec["outcome"] = {"flattened": not residual, "residual": residual, "halted": True}
        _print(rec["outcome"])
        return 0 if not residual else 1
    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "Path"]
