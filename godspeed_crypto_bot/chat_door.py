"""The chat door: the person's assistant brings a strategy to the bot through files, never through a shell.

On the one-line Docker server Godspeed's assistant lives in its container and cannot run commands on the server, but
it can write in its mission control folder. So the installer makes a `crypto-bot` folder there:

    crypto-bot/strategies/<name>.toml     the person's strategy files (the assistant writes them; sync may carry them)
    crypto-bot/requests/<id>.json         one small request each: {"request": "check" | "backtest" | "use-on-paper",
                                          "strategy": "<name>", and for a backtest optionally "start"/"end"}
    crypto-bot/requests/<id>.result.md    the answer in plain words, and <id>.result.json the same as data
    crypto-bot/.gitignore                 keeps requests/ out of a mission control folder that is a git repository

The root relay (every minute) answers the requests. Nothing written there is executed: a request is JSON checked against
a fixed list of kinds and names, a strategy file is settings checked against the menu (strategy_file.py), and a
malformed request is answered with one sentence and changes nothing. A strategy reaches the bot's own strategies folder
only after its check passed. Only paper actions go through this door: `use-on-paper` switches the strategy only in
paper or dry-run mode; real money, the cap, the account and the key are never reachable from here.

The relay is root and these folders belong to the assistant (on Docker to a user inside the container), so every
folder below the mission control folder is opened without following links and must belong to the folder's owner, files
are read only when that owner owns them, and what the relay writes there it writes as that owner. The relay so never
does in those folders more than their owner could do.

Every request and its outcome is a line in the relay's log (chat-requests.jsonl), which the bot copies into its own
records (table chat_requests) the next time any of its commands runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from datetime import date
from pathlib import Path

from . import configure, strategy_file
from .names import DISPLAY_NAME, NAME

FOLDER = "crypto-bot"
STRATEGIES = "strategies"
REQUESTS = "requests"
KINDS = ("check", "backtest", "use-on-paper")
REQUEST_NAME = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_-]{0,79})\.json$")
MAX_REQUEST = 4096
MAX_REPORT = 512 * 1024
PER_RUN = 5  # requests answered per relay run; a backtest ends the run's share
LOG_FILE = "chat-requests.jsonl"
RECIPE = "crypto-bot"
RECIPE_MARKER = f".installed-by-{NAME}"
GITIGNORE = f"# {DISPLAY_NAME}: requests and their answers stay on this server.\nrequests/\n"
README = (f"# {DISPLAY_NAME}\n\nYour own strategies for {DISPLAY_NAME} live in strategies/, one settings file each. "
          "Your assistant writes them with you and asks the bot to check, test or use one on paper through requests/; "
          "the bot answers within a minute beside each request. Real money is only ever switched on by you, typing "
          f"on the server: {NAME} go-live.\n")
WRAPPER = f"/usr/local/sbin/{NAME}"
POSIX_FDS = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


class Refused(Exception):
    """One plain sentence (or several, one per problem); nothing was changed."""

    def __init__(self, *sentences):
        self.sentences = [s for s in sentences if s]
        super().__init__(" ".join(self.sentences))


# ---- folders the relay must not be tricked in


class SafeDir:
    """A directory opened without following links, below a trusted base, owned by one user.

    Every name inside is a single path component. On a system without directory file descriptors (the tests on
    Windows) the same checks run on paths."""

    def __init__(self, path: Path, fd, uid, gid):
        self.path, self.fd, self.uid, self.gid = Path(path), fd, uid, gid

    @classmethod
    def open_base(cls, base) -> SafeDir:
        """The trusted starting point (the Godspeed folder as configured, or the Docker volume's own folder), resolved
        once: a Godspeed folder reached through a link is still the person's. Everything below it is walked without
        following links and must belong to the same user, so a redirected base can only lead into that user's own
        folders."""
        base = Path(os.path.realpath(base))
        st = os.lstat(base)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{base} is not a plain folder.")
        fd = os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)) if POSIX_FDS else None
        if fd is not None:
            st = os.fstat(fd)
        return cls(base, fd, st.st_uid, st.st_gid)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def _name(name):
        if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
            raise Refused(f"{name!r} is not a plain file name.")
        return name

    def _lstat(self, name):
        if self.fd is not None:
            return os.lstat(self._name(name), dir_fd=self.fd)
        return os.lstat(self.path / self._name(name))

    def exists(self, name):
        try:
            self._lstat(name)
            return True
        except FileNotFoundError:
            return False

    def sub(self, name, create=False) -> SafeDir:
        """A folder inside, owned by the same user, never through a link; made (as that user) when ``create``."""
        try:
            st = self._lstat(name)
        except FileNotFoundError:
            if not create:
                raise
            if self.fd is not None:
                os.mkdir(name, 0o775, dir_fd=self.fd)
                os.chown(name, self.uid, self.gid, dir_fd=self.fd, follow_symlinks=False)
            else:
                (self.path / name).mkdir(mode=0o775)
            st = self._lstat(name)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{self.path / name} is not a plain folder.")
        if st.st_uid != self.uid:
            raise Refused(f"{self.path / name} belongs to another user than {self.path}.")
        if self.fd is not None:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
            if os.fstat(fd).st_uid != self.uid:
                os.close(fd)
                raise Refused(f"{self.path / name} belongs to another user than {self.path}.")
            return SafeDir(self.path / name, fd, self.uid, self.gid)
        return SafeDir(self.path / name, None, self.uid, self.gid)

    def names(self):
        return sorted(os.listdir(self.fd if self.fd is not None else self.path))

    def read(self, name, limit) -> bytes:
        """A small regular file inside, owned by the folder's owner, never through a link."""
        self._name(name)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(name, flags, dir_fd=self.fd) if self.fd is not None else os.open(self.path / name, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise Refused(f"{name} cannot be read ({exc.strerror or exc}).") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self.uid:
                raise Refused(f"{name} is not a plain file of the folder's owner.")
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise Refused(f"{name} is larger than {limit // 1024} KB.")
        return data

    def write(self, name, data: bytes, mode=0o664):
        """Write a file inside as the folder's owner: a new temporary file, then renamed over the name (which replaces a
        link there rather than following it)."""
        self._name(name)
        tmp = f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if self.fd is not None:
            fd = os.open(tmp, flags, mode, dir_fd=self.fd)
            try:
                os.fchown(fd, self.uid, self.gid)
                os.write(fd, data)
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            p = self.path / tmp
            fd = os.open(p, flags, mode)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(p, self.path / name)

    def remove_tree(self, name):
        if self.fd is not None:
            shutil.rmtree(self._name(name), dir_fd=self.fd)
        else:
            shutil.rmtree(self.path / self._name(name))


def open_chain(base, parts) -> SafeDir:
    """The trusted base, then each part below it as a SafeDir (every one owned by the base's owner)."""
    d = SafeDir.open_base(base)
    for part in parts:
        nxt = d.sub(part)
        d.close()
        d = nxt
    return d


# ---- the request


def parse_request(data: bytes):
    """(kind, strategy name, start, end) from a request's bytes, or Refused with one sentence."""
    example = '{"request": "check", "strategy": "my-trend"}'
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused(f"This request is not JSON; write it like {example}. Nothing was changed.") from None
    if not isinstance(doc, dict):
        raise Refused(f"This request is not a JSON object like {example}. Nothing was changed.")
    unknown = sorted(str(k) for k in doc if k not in ("request", "strategy", "start", "end"))
    if unknown:
        raise Refused(f"This request has settings the bot does not know ({', '.join(unknown)[:80]}); a request has "
                      "request, strategy and, for a backtest, start and end. Nothing was changed.")
    kind = doc.get("request")
    if kind not in KINDS:
        raise Refused('The request must be "check", "backtest" or "use-on-paper". Nothing was changed.')
    name = doc.get("strategy")
    if not isinstance(name, str) or not strategy_file.NAME_RE.match(name):
        raise Refused("The request must name a strategy file in crypto-bot/strategies by its name, like "
                      '"strategy": "my-trend" for my-trend.toml. Nothing was changed.')
    dates = {}
    for key in ("start", "end"):
        if key in doc:
            if kind != "backtest":
                raise Refused(f"Only a backtest takes a {key} day. Nothing was changed.")
            try:
                dates[key] = date.fromisoformat(doc[key]) if isinstance(doc[key], str) and len(doc[key]) == 10 \
                    else None
            except ValueError:
                dates[key] = None
            if dates[key] is None:
                raise Refused(f"The {key} day must be written YYYY-MM-DD. Nothing was changed.")
    return kind, name, dates.get("start"), dates.get("end")


# ---- answering requests (the root relay)


def _sh(argv, timeout=120):
    from .godspeed import sh

    return sh(argv, timeout=timeout)


def read_bot_file(path: Path, limit=MAX_REPORT) -> str:
    """A report the bot's user wrote in its state folder: never through a link, and small."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError(f"{path} is not a plain file")
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise OSError(f"{path} is too large")
    return data.decode("utf-8", errors="replace")


def run_backtest(cfg, name, start, end, out_name, run=_sh):
    """The backtest as the bot's own user, through the wrapper, like any of its commands. Returns its Markdown report,
    or raises Refused with its one sentence."""
    argv = [WRAPPER, "backtest", f"file:{name}", "--out-name", out_name]
    if start:
        argv += ["--start", start.isoformat()]
    if end:
        argv += ["--end", end.isoformat()]
    code, _out, err = run(argv, timeout=1500)
    if code != 0:
        said = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
        sentence = said[-1].removeprefix(f"{NAME}: ") if said else "The test stopped without a report."
        raise Refused(sentence)
    try:
        return read_bot_file(cfg.state_path / "backtests" / f"{out_name}.md")
    except OSError:
        raise Refused("The test finished but its report could not be read; ask for it again.") from None


class Door:
    """Answers the requests in the assistant's crypto-bot folder: at most PER_RUN a run, oldest first, one backtest."""

    def __init__(self, cfg, config_path, root, perps, backtest=run_backtest, now=time.time):
        self.cfg, self.config_path, self.root = cfg, Path(config_path), root  # root: (trusted base, [parts])
        self.perps, self.backtest, self.now = perps, backtest, now
        self.log = []

    def __call__(self):
        base, parts = self.root
        try:
            folder = open_chain(base, [*parts, FOLDER])
        except FileNotFoundError:
            return {"answered": [], "problem": f"there is no {FOLDER} folder for the assistant (run the install line "
                                               "again to make it)"}
        except (Refused, OSError) as exc:
            return {"answered": [], "problem": f"the {FOLDER} folder is not usable: {exc}"}
        answered, opened = [], []
        try:
            try:
                for sub in (REQUESTS, STRATEGIES):
                    opened.append(folder.sub(sub, create=True))
            except (Refused, OSError) as exc:
                return {"answered": [], "problem": f"the {FOLDER} folder is not usable: {exc}"}
            requests_dir, strategies_dir = opened
            waiting = sorted((requests_dir._lstat(n).st_mtime, n) for n in requests_dir.names()
                             if REQUEST_NAME.match(n) and not requests_dir.exists(n[:-5] + ".result.json"))
            for _mtime, name in waiting[:PER_RUN]:  # the oldest first
                kind = self.answer(requests_dir, strategies_dir, name)
                answered.append(name)
                if kind == "backtest":
                    break
        finally:
            for d in opened:
                d.close()
            folder.close()
        return {"answered": answered}

    def answer(self, requests_dir, strategies_dir, name):
        rid = REQUEST_NAME.match(name)["id"]
        kind = strategy = fingerprint = None
        try:
            kind, strategy, start, end = parse_request(requests_dir.read(name, MAX_REQUEST))
            spec, data = self.check(strategies_dir, strategy)
            fingerprint = spec.fingerprint
            if kind == "use-on-paper" and self.cfg.mode not in ("paper", "dry-run"):
                raise Refused(f"{DISPLAY_NAME} is trading real money, so its strategy is not changed from the chat. To "
                              f"change it, the person types on the server: {NAME} configure --strategy file:{strategy} "
                              f"and then {NAME} go-live, which asks them to type a new confirmation sentence naming "
                              "this strategy. Nothing was changed.")
            self.install(spec, data, kind)
            if kind == "check":
                text = (f"Checked: the strategy file {strategy}.toml can be used (fingerprint {fingerprint}).\n\n"
                        f"What {DISPLAY_NAME} understood: {strategy_file.explain(spec)}\n\n"
                        "Next it can be tested against the past (a backtest request), and run on paper once the "
                        "person says yes (a use-on-paper request).\n")
                said = f"Checked: {strategy} can be used."
            elif kind == "backtest":
                report = self.backtest(self.cfg, strategy, start, end, f"chat-{rid}")
                text = f"Tested: the strategy file {strategy}.toml (fingerprint {fingerprint}).\n\n" + report
                said = f"Tested: {strategy}."
            else:
                configure.set_value(self.config_path, "strategy", spec.setting)
                text = (f"Done: from the next daily run (00:02 UTC) {DISPLAY_NAME} runs your own strategy "
                        f'"{strategy}" (fingerprint {fingerprint}) on paper, with pretend money on real prices. '
                        f"{strategy_file.explain(spec)}\n")
                said = f"Done: {strategy} runs on paper from the next daily run."
            outcome = "done"
        except Refused as exc:
            outcome, said = "refused", exc.sentences[0] if exc.sentences else "Refused."
            lines = exc.sentences
            text = ("Refused: " + lines[0] + "\n") if len(lines) == 1 else (
                "Refused, for these reasons:\n" + "".join(f"- {s}\n" for s in lines) + "Nothing was changed.\n")
        except Exception as exc:  # noqa: BLE001 - an answer is always written, and nothing half-done stays
            outcome, said = "failed", f"The bot could not answer this request ({type(exc).__name__}); ask again."
            text = said + "\n"
        doc = {"request": rid, "kind": kind, "strategy": strategy, "fingerprint": fingerprint, "outcome": outcome,
               "said": said, "at": self.now(), "mode": self.cfg.mode, "network": self.cfg.network}
        requests_dir.write(f"{rid}.result.md", text.encode("utf-8"))
        requests_dir.write(f"{rid}.result.json", (json.dumps(doc, indent=1) + "\n").encode("utf-8"))
        self.log.append(doc)
        append_log(self.cfg, doc)
        return kind

    def check(self, strategies_dir, name):
        file = f"{name}.toml"
        try:
            data = strategies_dir.read(file, strategy_file.MAX_FILE_BYTES)
        except FileNotFoundError:
            raise Refused(f"There is no strategy file {file} in crypto-bot/strategies. Nothing was changed.") from None
        try:
            perps = self.perps()
        except Exception as exc:  # noqa: BLE001 - the coins cannot be checked: nothing is accepted
            raise Refused(f"Hyperliquid could not be reached to check that the coins exist ({type(exc).__name__}); "
                          "ask again in a minute. Nothing was changed.") from None
        try:
            return strategy_file.check_bytes(data, file_name=name, perps=perps), data
        except strategy_file.StrategyFileError as exc:
            raise Refused(*exc.problems) from None

    def install(self, spec, data, kind):
        """The checked file into the bot's own strategies folder, unless that would change the strategy the bot runs
        now without the person's yes to running it (a use-on-paper request)."""
        target = strategy_file.path_for(self.cfg.strategies_dir, spec.name)
        if self.cfg.strategy == spec.setting and kind != "use-on-paper" and target.exists():
            try:
                running = strategy_file.fingerprint(target.read_bytes())
            except OSError:
                running = None
            if running != spec.fingerprint:
                raise Refused(f'{DISPLAY_NAME} runs "{spec.name}" now, and this file is a changed version of it. To '
                              f"check or test the change, save it under its own name (for example {spec.name}-2); to "
                              "run it, ask to use it on paper. Nothing was changed.")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)


def append_log(cfg, doc):
    try:
        cfg.relay_path.mkdir(parents=True, exist_ok=True)
        with open(cfg.relay_path / LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, sort_keys=True) + "\n")
    except OSError:
        pass  # the answer beside the request is what matters; the records copy what the log kept


def import_log(cfg, store):
    """Copy the relay's log of chat requests into the bot's records (run by the bot's own user)."""
    try:
        lines = (cfg.relay_path / LOG_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    added = 0
    for line in lines:
        try:
            doc = json.loads(line)
            added += store.add_chat_request(doc)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return added


# ---- setting up the folder and the recipe (root, by the installer)


def recipe_room(ws: SafeDir):
    """Where the assistant reads recipes: skills/, or .claude/skills/ when only that holds recipes."""
    def has_recipes(d):
        try:
            sub = ws.sub(*d) if len(d) == 1 else ws.sub(d[0]).sub(d[1])
        except (FileNotFoundError, Refused):
            return False
        try:
            return any(sub.exists(n) and _is_recipe(sub, n) for n in sub.names())
        finally:
            sub.close()

    if not has_recipes(("skills",)) and has_recipes((".claude", "skills")):
        return (".claude", "skills")
    return ("skills",)


def _is_recipe(d, name):
    try:
        sub = d.sub(name)
    except (Refused, OSError):
        return False
    try:
        return sub.exists("SKILL.md")
    finally:
        sub.close()


def setup(root, code_dir, version, old_places=()):
    """Make the crypto-bot folder and put the recipe where the assistant reads recipes, as the folder's owner. Returns
    plain sentences of what was done. ``root``: (trusted base, [parts]) of the assistant's mission control folder."""
    base, parts = root
    said = []
    ws = open_chain(base, parts)
    try:
        folder = ws.sub(FOLDER, create=True)
        try:
            for sub in (STRATEGIES, REQUESTS):
                folder.sub(sub, create=True).close()
            if not folder.exists(".gitignore"):
                folder.write(".gitignore", GITIGNORE.encode())
            if not folder.exists("README.md"):
                folder.write("README.md", README.encode())
        finally:
            folder.close()
        said.append(f"The assistant's folder for strategies and requests: {ws.path / FOLDER}")
        room_parts = recipe_room(ws)
        room = ws
        opened = []
        for part in room_parts:
            room = room.sub(part, create=True)
            opened.append(room)
        try:
            if room.exists(RECIPE) and not _marked(room, RECIPE):
                said.append(f"Left alone: {room.path / RECIPE} holds a recipe you wrote with the same name.")
            else:
                if room.exists(RECIPE):
                    room.remove_tree(RECIPE)
                dst = room.sub(RECIPE, create=True)
                try:
                    src = Path(code_dir) / "skill" / RECIPE
                    for f in sorted(src.iterdir()):
                        if f.is_file():
                            dst.write(f.name, f.read_bytes())
                    ref = Path(code_dir) / "docs" / "strategy-files.md"
                    if ref.is_file():
                        dst.write("strategy-files.md", ref.read_bytes())
                    dst.write(RECIPE_MARKER, f"{NAME} {version}\n".encode())
                finally:
                    dst.close()
                said.append(f"The recipe for the assistant: {room.path / RECIPE}")
        finally:
            for d in reversed(opened):
                d.close()
    finally:
        ws.close()
    for place in old_places:  # where an earlier version put the recipe and nobody reads it
        p = Path(place)
        if (p / RECIPE_MARKER).is_file() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
    return said


def _marked(room, name):
    try:
        d = room.sub(name)
    except (Refused, OSError):
        return False
    try:
        return d.exists(RECIPE_MARKER)
    finally:
        d.close()


__all__ = ["Door", "Refused", "SafeDir", "import_log", "open_chain", "parse_request", "run_backtest", "setup"]
