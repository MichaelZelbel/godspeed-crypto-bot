"""Everything that reaches the owner's Godspeed Mission Control: the relay.

Godspeed is installed natively or with the one-line Docker installer. The bot works with both through the
same three doors, all used by Godspeed's other add-ons (docs/full-version/addon-model.md in the product):

- the assistant's Telegram bot, for messages (the token is read from the assistant's .env each time and
  never copied anywhere);
- the add-on deadline door (`addon-deadlines`), for the reminder to renew the exchange key;
- the notebook's web address, for the page: a path file in Caddy's add-on folder behind the owner's sign-in
  (native), or a Caddy container of the add-on's own that shares Godspeed's certificate (Docker, whose Caddy
  runs in a container and reads no folder).

Only root can reach all three (the assistant's .env, the Docker socket, Caddy), and the trading programs run
as an unprivileged user, so they write to the outbox (outbox.py) and `godspeed-crypto-bot relay` carries it
out. The relay never trades and never touches the key. Every outside call is injectable for the tests.

The relay also answers the chat door (chat_door.py): the requests Godspeed's assistant writes in its own mission
control folder. `assistant_root` finds that folder on the host: the Godspeed folder on a native install, and on the
one-line Docker server the workspace inside Godspeed's container, through the container's mounts (a Docker volume).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import chat_door, outbox
from .names import DISPLAY_NAME, NAME, PAGE_DIR

log = logging.getLogger(__name__)

NATIVE_DOOR = "http://127.0.0.1:47831"
ADDON_DIR = "/etc/caddy/godspeed-addons"
OWN_UNIT = "godspeed-https.service"
SERVER_NOTEBOOK_SITE = "/etc/caddy/godspeed-notebook.caddy"
DOCKER_FRONT = f"{NAME}-https"
DOCKER_PORT = 48445
ADDRESS_FILE = "page-address"
PAGE_TRIED = "page-tried.json"  # an attempt Caddy did not take: the same one is never made again (no restart loop)
DEADLINE_SYNCED = "deadline-synced.json"
STATUS_FILE = "relay-status.json"
CHANNEL_FILE = "channel.json"
CHANNEL_RECHECK_S = 600  # whether a channel is there is looked at again this often (a message waiting tries it at once)
NO_CHANNEL = (f"your Godspeed has no Telegram connected, so {DISPLAY_NAME}'s messages wait until Telegram is "
              "connected there")
PAGE_RECHECK_S = 600  # the page address is looked at again this often (a Godspeed update may take it away)
DEADLINE_RECHECK_S = 6 * 3600  # Godspeed is asked again that often whether the reminder is still there
SENDS_PER_RELAY = 50
KEY_DEADLINE_KEY = "exchange-key"
CHAT_ROOT_FILE = "chat-root.json"
CHAT_ROOT_RECHECK_S = 600  # where the assistant's folder is on the host is looked up again this often


def sh(argv, timeout=120, stdin=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


# ---- which Godspeed


@dataclass
class Godspeed:
    root: str  # the Godspeed folder
    docker: dict | None = None  # {"compose", "service", "workspace"} for the one-line Docker installation

    @property
    def kind(self):
        return "docker" if self.docker else "native"


def _absolute(p):
    return isinstance(p, str) and os.path.isabs(p) and ".." not in Path(p).parts


def find(godspeed_dir) -> Godspeed | None:
    """The installation at the given folder: Docker when the installer's marker is there, else native."""
    if not _absolute(godspeed_dir) or not Path(godspeed_dir).is_dir():
        return None
    try:
        marker = json.loads((Path(godspeed_dir) / ".godspeed" / "model-host.json").read_text(encoding="utf-8"))
        compose = Path(godspeed_dir) / "compose.yaml"
        if (marker.get("format") == 1 and marker.get("kind") == "godspeed-docker"
                and marker.get("service") == "godspeed" and _absolute(marker.get("workspace")) and compose.is_file()):
            return Godspeed(godspeed_dir, {"compose": str(compose), "service": "godspeed",
                                           "workspace": marker["workspace"]})
    except (OSError, ValueError):
        pass
    return Godspeed(godspeed_dir)


def assistant_root(gs, run=sh):
    """(trusted base, [folders below it]) of the mission control folder Godspeed's assistant works in, on this host,
    or None when it cannot be found. The folders below the base are walked without following links (chat_door)."""
    if not gs.docker:
        return Path(gs.root), []
    code, out, _err = run(compose_args(gs) + ["ps", "-q", gs.docker["service"]], timeout=60)
    ids = (out or "").split() if code == 0 else []
    if not ids:
        return None
    code, out, _err = run(["docker", "inspect", ids[0]], timeout=60)
    try:
        mounts = json.loads(out)[0]["Mounts"] if code == 0 else []
    except (ValueError, LookupError, TypeError):
        return None
    workspace, best = gs.docker["workspace"], None
    for m in mounts if isinstance(mounts, list) else []:
        dest, src = (m.get("Destination"), m.get("Source")) if isinstance(m, dict) else (None, None)
        if not (_absolute(dest) and _absolute(src)):
            continue
        if (workspace == dest or workspace.startswith(dest.rstrip("/") + "/")) and (
                best is None or len(dest) > len(best[0])):
            best = (dest, src)
    if best is None:
        return None
    rest = [p for p in workspace[len(best[0]):].split("/") if p]
    if any(p in (".", "..") for p in rest):
        return None
    return Path(best[1]), rest


def compose_args(gs):
    return ["docker", "compose", "-f", gs.docker["compose"], "--project-directory", gs.root]


def exec_args(gs, args):
    return compose_args(gs) + ["exec", "-T", "--user", "hermes", gs.docker["service"], *args]


# The Docker door: the request goes into Godspeed's container, which asks its own loopback-only add-on port
# (GODSPEED_LOCAL_PORT, 47832), the way Godspeed's other add-ons do.
RELAY_JS = (
    "let raw='';for await(const c of process.stdin)raw+=c;const q=JSON.parse(raw);"
    "if(!['/health','/api/functions/addon-deadlines'].includes(q.path))throw Error('unknown route');"
    "const m=q.path==='/health'?'GET':'POST';"
    "const r=await fetch('http://127.0.0.1:47832'+q.path,{method:m,headers:{'content-type':'application/json'},"
    "...(m==='POST'?{body:q.body}:{}),signal:AbortSignal.timeout(30000)});"
    "console.log(JSON.stringify({status:r.status,body:await r.text()}));"
)


class Door:
    """Godspeed's notebook door for add-ons, native or through the Docker service."""

    def __init__(self, gs, run=sh, urlopen=urllib.request.urlopen, base=None):
        self.gs, self.run, self.urlopen = gs, run, urlopen
        self.base = base or NATIVE_DOOR

    def request(self, path, body=None):
        """(status, text). body is a dict for POST, None for GET."""
        if self.gs.docker:
            payload = json.dumps({"path": path, "body": json.dumps(body) if body is not None else None})
            code, out, _err = self.run(exec_args(self.gs, ["node", "--input-type=module", "-e", RELAY_JS]),
                                       timeout=60, stdin=payload)
            if code != 0:
                raise OSError("Godspeed's Docker service did not answer")
            ans = json.loads(out)
            return ans["status"], ans["body"]
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET",
                                     headers={"content-type": "application/json"})
        try:
            with self.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def deadlines(self, body):
        status, text = self.request("/api/functions/addon-deadlines", {"addon": NAME, **body})
        try:
            doc = json.loads(text)
        except ValueError:
            doc = {}
        if status != 200:
            raise RuntimeError(doc.get("error") or f"the deadline door answered HTTP {status}")
        return doc.get("data", doc)

    def version(self):
        try:
            status, text = self.request("/health")
            return json.loads(text).get("version") if status == 200 else None
        except (OSError, ValueError, KeyError):
            return None


_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+.*)?$")


def _version_key(v):
    m = _VERSION.match(str(v or ""))
    if not m:
        return None
    major, minor, patch = (int(x) for x in m.groups()[:3])
    pre = m.group(4)
    parts = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in (pre.split(".") if pre else [])]
    return (major, minor, patch, 0 if pre else 1, parts)  # a release sorts after its own prereleases


def version_ok(version, minimum) -> bool:
    a, b = _version_key(version), _version_key(minimum)
    return a is not None and b is not None and a >= b


def native_version(godspeed_dir):
    """The version of a native installation from its files (no sign-in needed), else None."""
    try:
        runtime = Path(godspeed_dir) / ".godspeed" / "integrated-runtime"
        ref = (runtime / "version").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-f0-9]{40}", ref):
            pkg = runtime / "versions" / ref / "notebook" / "package.json"
            return json.loads(pkg.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        pass
    return None


def godspeed_version(gs, door=None):
    if gs.kind == "native":
        v = native_version(gs.root)
        if v:
            return v
    return (door or Door(gs)).version()


# ---- the assistant's Telegram bot


def _parse_env(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = re.sub(r"^[\"']|[\"']$", "", m.group(2))
    return out


def telegram_credentials(env_text):
    env = _parse_env(env_text or "")
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_TOKEN") or ""
    allowed = [x for x in re.split(r"[,\s]+", env.get("TELEGRAM_ALLOWED_USERS", "")) if x]
    chat = (env.get("TELEGRAM_HOME_CHANNEL") or env.get("TELEGRAM_CHAT_ID") or env.get("CHAT_ID")
            or (allowed[0] if len(allowed) == 1 else ""))
    return {"token": token, "chat": str(chat)} if token and chat else None


def valid_hermes_env(p):
    """Only a Hermes home's own .env is ever read: .../hermes/.env, .../.hermes/.env, a profile's."""
    if not _absolute(p):
        return False
    seg = [s for s in Path(p).parts if s not in ("/", "\\")]

    def home(s):
        return s.lower() in (".hermes", "hermes", "hermes-profile")

    if len(seg) < 2 or seg[-1] != ".env":
        return False
    return home(seg[-2]) or (len(seg) >= 4 and seg[-3] == "profiles" and home(seg[-4]))


def env_candidates(cfg, env=os.environ):
    out = []
    if cfg.godspeed_dir:
        root = Path(cfg.godspeed_dir)
        try:
            a = json.loads((root / ".godspeed" / "assistant.json").read_text(encoding="utf-8"))
            if a.get("verified") and _absolute(a.get("home")):
                out.append(str(Path(a["home"]) / ".env"))
        except (OSError, ValueError):
            pass
        out.append(str(root / ".godspeed" / "integrated-runtime" / "hermes" / ".env"))
    if env.get("HERMES_HOME"):
        out.append(str(Path(env["HERMES_HOME"]) / ".env"))
    dot = Path(os.path.expanduser("~")) / ".hermes"
    out.append(str(dot / ".env"))
    try:
        out += [str(p / ".env") for p in sorted((dot / "profiles").iterdir())]
    except OSError:
        pass
    return list(dict.fromkeys(out))


DOCKER_ENV_JS = (
    "const fs=require('node:fs'),path=require('node:path');"
    "const d=JSON.parse(fs.readFileSync(path.join(process.argv[1],'.godspeed/assistant.json'),'utf8'));"
    "if(!d.verified||!path.isAbsolute(d.home))throw Error('No verified assistant');"
    "process.stdout.write(fs.readFileSync(path.join(d.home,'.env'),'utf8'));"
)


def find_telegram(cfg, gs=None, run=sh, env=os.environ):
    """The bot's token and chat, read fresh. In a Docker installation from the assistant's own home inside the
    container; no unrelated host bot is tried there."""
    if cfg.telegram_env and valid_hermes_env(cfg.telegram_env):
        try:
            return telegram_credentials(Path(cfg.telegram_env).read_text(encoding="utf-8"))
        except OSError:
            return None
    if gs is not None and gs.docker:
        code, out, _ = run(exec_args(gs, ["node", "-e", DOCKER_ENV_JS, gs.docker["workspace"]]), timeout=60)
        return telegram_credentials(out) if code == 0 else None
    for f in env_candidates(cfg, env):
        if valid_hermes_env(f):
            try:
                c = telegram_credentials(Path(f).read_text(encoding="utf-8"))
            except OSError:
                continue
            if c:
                return c
    return None


def telegram_base(env=os.environ):
    if env.get("GODSPEED_CRYPTO_BOT_TELEGRAM_API"):
        if env.get("GODSPEED_CRYPTO_BOT_TEST") != "1":
            raise RuntimeError("a pretend Telegram address is honoured only in test mode")
        return env["GODSPEED_CRYPTO_BOT_TELEGRAM_API"].rstrip("/")
    return "https://api.telegram.org"


def send_telegram(creds, text, env=os.environ, urlopen=urllib.request.urlopen, sleep=time.sleep):
    """Asked again only when Telegram surely did not take the message (no connection, busy, a server error);
    never after no answer in time, because the message may have arrived and must not arrive twice."""
    url = f"{telegram_base(env)}/bot{creds['token']}/sendMessage"
    body = json.dumps({"chat_id": creds["chat"], "text": text[:4000], "disable_web_page_preview": True}).encode()
    last = RuntimeError("Telegram did not take the message")
    for i in range(3):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"content-type": "application/json"})
            with urlopen(req, timeout=30) as r:
                if json.loads(r.read().decode()).get("ok") is True:
                    return True
            last = RuntimeError("Telegram did not take the message")
        except urllib.error.HTTPError as exc:
            last = RuntimeError(f"Telegram refused the message (HTTP {exc.code})")
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except (TimeoutError, OSError) as exc:  # urllib wraps a timeout in URLError, itself an OSError
            last = exc
            if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                break
        if i < 2:
            sleep(2 * (i + 1))
    raise last


# ---- the key-renewal reminder (the add-on deadline door)

RENEW_STEPS = (
    f"A new API wallet for {DISPLAY_NAME} is made (`{NAME} keygen --network mainnet --replace`), its address is "
    "approved on Hyperliquid's API page (app.hyperliquid.xyz/API), `go-live` is run again by the owner "
    f"(its sentence names the key) and `{NAME} preflight` is clean."
)


def key_deadline(expires_on, network="mainnet"):
    """The wanted reminder for a key that expires on that date (a datetime.date)."""
    return {"key": f"{KEY_DEADLINE_KEY}-{network}", "due": expires_on.isoformat(),
            "title": f"Renew {DISPLAY_NAME}'s exchange key before it expires",
            "done_when": RENEW_STEPS, "cost": f"{DISPLAY_NAME} stops trading when the key expires."}


def sync_deadline(door, want, synced, now=None):
    """Create the reminder, or move its date when the key was renewed. Returns the record to remember as
    `synced` (the wanted document plus what Godspeed said), or `synced` itself when nothing is to be done.
    Godspeed is asked again every few hours even when nothing changed, so a reminder lost there comes back."""
    now = now or time.time
    if synced and synced.get("want") == want and now() - synced.get("at", 0) < DEADLINE_RECHECK_S:
        return synced
    listed = door.deadlines({"action": "list"}).get("deadlines", [])
    existing = next((d for d in listed if d.get("key") == want["key"]), None)
    if existing is None:
        ans = door.deadlines({"action": "create", **want})
        said = "created" if ans.get("created") else "already there"
    elif existing.get("due") == want["due"]:
        said = "already right"
    else:
        ans = door.deadlines({"action": "update", "key": want["key"], "due": want["due"]})
        said = "date moved" if ans.get("updated") else ("yours (you changed it)" if ans.get("touched") else "not moved")
    return {"want": want, "said": said, "at": now()}


# ---- the page's web address


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8")
    except OSError:
        return None


def first_site(text):
    """(host, port or None) of the first site line, e.g. 'srv1.example.com:48443 {'."""
    for line in str(text or "").splitlines():
        m = re.match(r"^\s*(?:https://)?([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(\d+))?\s*\{\s*$", line, re.I)
        if m and "." in m.group(1):
            return m.group(1).lower(), m.group(2)
    return None


def path_site(upstream):
    return (f"# {DISPLAY_NAME}'s page, for whoever is signed in to Godspeed. {NAME} writes this file.\n"
            f"redir /{NAME} /{NAME}/ 308\n"
            f"handle_path /{NAME}/* {{\n"
            f"\tforward_auth 127.0.0.1:{upstream} {{\n\t\turi /api/session\n\t\t@signin status 401\n"
            "\t\thandle_response @signin {\n\t\t\tredir * / 302\n\t\t}\n\t}\n"
            f"\troot * {PAGE_DIR}\n\tfile_server\n}}\n")


def docker_site(host, port):
    return ("{\n\tadmin off\n\tauto_https disable_redirects\n}\n\n"
            f"# {DISPLAY_NAME}'s page, for whoever is signed in to Godspeed. {NAME} writes this file.\n"
            f"{host}:{port} {{\n\tforward_auth godspeed:47831 {{\n\t\turi /api/session\n\t\t@signin status 401\n"
            f"\t\thandle_response @signin {{\n\t\t\tredir * https://{host}/ 302\n\t\t}}\n\t}}\n"
            "\troot * /srv/page\n\tfile_server\n}\n")


def _write_keep(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


EXEC_PREFIXES = "-@:+!"  # what systemd allows before an ExecStart= path (systemd.service, "Command lines")


def exec_argv(exec_line):
    """An ExecStart= value as systemd splits it, or [] when it cannot be read: quotes and backslash escapes
    honoured (Godspeed's installer writes the Caddy path in double quotes), and the special prefixes that may
    stand before the path (-, @, :, +, !, !!) taken off. With @ the second word is only the name the program
    is shown under, so it is dropped too, and what is left is the path and its real arguments."""
    line = str(exec_line or "").strip()
    flags = ""
    while line[:1] and line[:1] in EXEC_PREFIXES:
        flags, line = flags + line[0], line[1:]
    try:
        argv = shlex.split(line)
    except ValueError:  # an unbalanced quote: better no answer than a wrong program
        return []
    if "@" in flags and len(argv) >= 2:
        argv = [argv[0], *argv[2:]]
    return argv


def _config_arg(argv):
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
    return None


def native_front(root="/", run=sh):
    """The Caddy that holds the notebook's address, as {'kind', 'unit', 'caddyfile', 'text', 'binary'}, or None:
    Godspeed's own (godspeed-https.service) first, then the server's (caddy.service)."""

    def at(p):
        return Path(root) / str(p).lstrip("/")

    unit = _read(at(f"/etc/systemd/system/{OWN_UNIT}"))
    if unit:
        exec_line = (re.search(r"^ExecStart=(.*)$", unit, re.M) or [None, ""])[1]
        argv = exec_argv(exec_line)
        caddyfile = _config_arg(argv)
        text = _read(at(caddyfile)) if caddyfile else None
        if text is not None:
            return {"kind": "own", "unit": OWN_UNIT, "caddyfile": caddyfile, "text": text, "binary": argv[0]}
    if run(["systemctl", "is-active", "--quiet", "caddy"])[0] == 0:
        text = _read(at(SERVER_NOTEBOOK_SITE))
        if text is not None:
            return {"kind": "caddy", "unit": "caddy.service", "caddyfile": "/etc/caddy/Caddyfile", "text": text,
                    "binary": "caddy"}
    return None


def _attempt_key(front, want):
    """What one attempt to give the page its address depends on: the file and the Caddy that would read it. A
    Godspeed update that changes its Caddy changes the key, and the page's address is tried once more then."""
    parts = (want, front["text"], front["binary"], front["caddyfile"], front["unit"])
    return hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()


def publish_native(front, root="/", run=sh, tried=None):
    """Write the path file and let Caddy take it. Returns (address or None, why).

    Godspeed's web server serves everything else on the server too, so it is restarted only when Caddy's own
    check accepted the new file, and at most once more to put back what it served before. An attempt that
    failed is kept in `tried` (a file in the relay's folder) and the same attempt is never made again, so no
    failure can make the relay restart Godspeed's web address over and over."""
    if "/paths/*.caddy" not in front["text"]:
        return None, ("Godspeed's web address does not read add-on files yet; update Godspeed Mission Control "
                      f"and {DISPLAY_NAME} will give its page an address")
    site = first_site(front["text"])
    up = re.search(r"reverse_proxy\s+127\.0\.0\.1:(\d+)", front["text"])
    if not site or not up:
        return None, "Godspeed's web address could not be read from its Caddy file"
    target = Path(root) / ADDON_DIR.lstrip("/") / "paths" / f"{NAME}.caddy"
    want = path_site(up.group(1))
    host, port = site
    address = f"https://{host}{':' + port if port and port != '443' else ''}/{NAME}/"
    before = _read(target)
    if before == want:
        if tried is not None:
            Path(tried).unlink(missing_ok=True)
        return address, ""
    key = _attempt_key(front, want)
    if tried is not None:
        last = _read(tried)
        try:
            last = json.loads(last) if last else None
        except ValueError:
            last = None
        if isinstance(last, dict) and last.get("key") == key:
            return None, (f"{last.get('why') or 'Caddy did not take it'}; it is tried again when Godspeed's web "
                          "address changes or the install line runs again")

    def put_back():
        if before is None:
            target.unlink(missing_ok=True)
        else:
            _write_keep(target, before)

    def note(why):
        """Keep the attempt, so the same one is never made again. False when the note could not be written."""
        if tried is None:
            return True
        try:
            _write_keep(Path(tried), json.dumps({"key": key, "why": why, "at": time.time()}))
            return True
        except OSError as exc:
            log.warning("the page attempt could not be noted: %s", exc)
            return False

    _write_keep(target, want)
    caddyfile = Path(root) / front["caddyfile"].lstrip("/")
    if run([front["binary"], "validate", "--config", str(caddyfile), "--adapter", "caddyfile"])[0] != 0:
        put_back()  # Caddy never read the file: taking it out again is all, and nothing is restarted
        why = "Caddy's own check did not accept the page's address, so nothing was changed"
        note(why)  # without the note the check runs again later, which restarts nothing either
        return None, why
    # Noted before the restart, so a relay stopped halfway, or a failed restart, can never lead to the same
    # restart again; the note goes once Caddy has taken the file. No note, no restart.
    why = "Caddy did not take the page's address, so it serves what it served before"
    if not note(why):
        put_back()
        return None, "the relay could not write in its own folder, so Godspeed's web address was not restarted"
    restart = ["systemctl", "restart", front["unit"]] if front["kind"] == "own" else ["systemctl", "reload", "caddy"]
    if run(restart)[0] != 0:  # take the file out again, and make Caddy read what it read before, once
        put_back()
        run(restart)
        return None, why
    if tried is not None:
        Path(tried).unlink(missing_ok=True)
    return address, ""


def docker_front(gs, run=sh):
    """The facts of Godspeed's Docker web front: {'image', 'storage': (type, source), 'network', 'host'}."""
    code, out, _ = run(compose_args(gs) + ["ps", "-q", "https"])
    cid = out.strip()
    if code != 0 or not re.fullmatch(r"[a-f0-9]{12,64}", cid):
        return None
    code, out, _ = run(["docker", "inspect", cid])
    try:
        c = json.loads(out)[0]
        data = next(m for m in c["Mounts"] if m["Destination"] == "/data")
        net = next(iter(c["NetworkSettings"]["Networks"]))
        env = _parse_env(_read(Path(gs.root) / ".env") or "")
        host = env.get("GODSPEED_HOST", "").strip()
        if not c["State"]["Running"] or data["Type"] not in ("volume", "bind") or not c["Config"]["Image"]:
            return None
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host, re.I):
            return None
        return {"image": c["Config"]["Image"], "network": net, "host": host,
                "storage": (data["Type"], data["Name"] if data["Type"] == "volume" else data["Source"])}
    except (ValueError, StopIteration, KeyError, IndexError, TypeError):
        return None


def publish_docker(gs, run=sh, config_dir=None):
    """Godspeed's Caddy runs in a container that reads no add-on folder, so the page gets a container of its
    own: the same Caddy image and certificate storage, on Godspeed's network (it asks Godspeed's own sign-in
    check there), serving the page folder read-only on one port of its own. Returns (address or None, why)."""
    front = docker_front(gs, run)
    if front is None:
        return None, "Godspeed's web front (its https container) was not found, so the page has no address yet"
    config_dir = Path(config_dir or f"{PAGE_DIR}-front")
    want = docker_site(front["host"], DOCKER_PORT)
    address = f"https://{front['host']}:{DOCKER_PORT}/"
    code, out, _ = run(["docker", "inspect", DOCKER_FRONT])
    old = None
    try:
        old = json.loads(out)[0] if code == 0 else None
    except ValueError:
        pass
    if old and old.get("Config", {}).get("Labels", {}).get("org.godspeed.addon") != NAME:
        return None, f"a container named {DOCKER_FRONT} belongs to something else; it was kept"
    if (old and old["State"]["Running"] and old["Config"]["Image"] == front["image"]
            and _read(config_dir / "Caddyfile") == want):
        return address, ""
    config_dir.mkdir(parents=True, exist_ok=True)
    _write_keep(config_dir / "Caddyfile", want)
    if old:
        run(["docker", "rm", "-f", DOCKER_FRONT])
    stype, source = front["storage"]
    code, _out, _err = run([
        "docker", "run", "-d", "--name", DOCKER_FRONT, "--restart", "unless-stopped",
        "--network", front["network"], "--label", f"org.godspeed.addon={NAME}", "-p", f"{DOCKER_PORT}:{DOCKER_PORT}",
        "--mount", f"type=bind,src={config_dir},dst=/etc/caddy,readonly",
        "--mount", f"type=bind,src={PAGE_DIR},dst=/srv/page,readonly",
        "--mount", f"type={stype},src={source},dst=/data", "-e", "XDG_DATA_HOME=/data", front["image"],
        "caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
    if code != 0:
        return None, f"the page's web container did not start (docker logs {DOCKER_FRONT})"
    ufw = run(["ufw", "status"])
    if ufw[0] == 0 and re.search(r"^Status: active", ufw[1], re.M):
        run(["ufw", "allow", f"{DOCKER_PORT}/tcp"])
    return address, ""


def publish_page(gs, run=sh, root="/", tried=None):
    if gs.docker:
        return publish_docker(gs, run)
    front = native_front(root, run)
    if front is None:
        return None, "no Caddy holds Godspeed's web address on this server, so the page has no address"
    return publish_native(front, root, run, tried)


def remove_page(gs, run=sh, root="/"):
    """Uninstall: take the page's address out of Godspeed's Caddy, and the add-on's own web container."""
    if gs is not None and gs.docker:
        code, out, _ = run(["docker", "inspect", DOCKER_FRONT])  # only a container this add-on made, never another
        try:
            ours = code == 0 and json.loads(out)[0].get("Config", {}).get("Labels", {}).get("org.godspeed.addon") == NAME
        except (ValueError, IndexError, AttributeError):
            ours = False
        if ours:
            run(["docker", "rm", "-f", DOCKER_FRONT])
        return
    target = Path(root) / ADDON_DIR.lstrip("/") / "paths" / f"{NAME}.caddy"
    if target.exists():
        front = native_front(root, run)
        target.unlink()
        if front:
            run(["systemctl", "restart", front["unit"]] if front["kind"] == "own" else ["systemctl", "reload", "caddy"])


# ---- the relay


def read_status(cfg):
    try:
        return json.loads((cfg.relay_path / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class Relay:
    cfg: object
    gs: Godspeed
    run: object = sh
    door: object = None
    tell: object = None  # tell(text) -> True, or raises; default: the assistant's Telegram bot
    root: str = "/"
    problems: list = field(default_factory=list)
    config_path: object = None  # the main configuration: only its relay answers the chat door
    perps: object = None  # perps() -> the perpetuals Hyperliquid lists (default: asked once per run)
    backtest: object = None  # the chat door's backtest runner (default: the bot's own, as its user)

    def _tell(self, text):
        if self.tell is not None:
            return self.tell(text)
        creds = find_telegram(self.cfg, self.gs, self.run)
        self._remember_channel(creds is not None)  # what a delivery saw is the freshest answer
        if not creds:
            raise RuntimeError(NO_CHANNEL)
        return send_telegram(creds, text)

    def deliver_alerts(self):
        sent = 0
        for path in outbox.waiting_alerts(self.cfg)[:SENDS_PER_RELAY]:
            doc = outbox.read_json(path)
            if not isinstance(doc, dict) or not isinstance(doc.get("text"), str):
                path.rename(path.with_suffix(".bad"))
                continue
            try:
                self._tell(doc["text"])
            except Exception as exc:  # noqa: BLE001 - kept in the outbox, tried again at the next relay
                self.problems.append(f"messages are waiting: {exc}")
                break
            path.unlink(missing_ok=True)
            sent += 1
        return sent

    def check_channel(self):
        """Whether a channel is there to take messages, also when none waits, so the owner hears of a missing one
        before a message needs it (status and the installer say so). Looked at every ten minutes."""
        if self.tell is not None:
            return True
        target = self.cfg.relay_path / CHANNEL_FILE
        try:
            if time.time() - target.stat().st_mtime < CHANNEL_RECHECK_S:
                return json.loads(target.read_text(encoding="utf-8")).get("telegram") is True
        except (OSError, ValueError, AttributeError):
            pass
        ok = find_telegram(self.cfg, self.gs, self.run) is not None
        self._remember_channel(ok)
        return ok

    def _remember_channel(self, ok):
        try:
            _write_keep(self.cfg.relay_path / CHANNEL_FILE, json.dumps({"telegram": ok}))
        except OSError as exc:
            log.warning("channel check not kept: %s", exc)

    def sync_deadline(self):
        want = outbox.read_json(outbox.directory(self.cfg) / outbox.DEADLINE_FILE)
        if not isinstance(want, dict):
            return None
        done = self.cfg.relay_path / DEADLINE_SYNCED
        try:
            synced = outbox.read_json(done)
            synced = synced if isinstance(synced, dict) else None
            result = sync_deadline(self.door or Door(self.gs, self.run), want, synced)
            if result is not synced:
                _write_keep(done, json.dumps(result))
            return result["said"]
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"the key reminder could not be kept in Godspeed: {exc}")
            return None

    def publish_page(self):
        if not Path(PAGE_DIR).is_dir():
            return None
        target = self.cfg.relay_path / ADDRESS_FILE
        try:  # looked at again every ten minutes (it puts back what a Godspeed update took away), not every minute
            if time.time() - target.stat().st_mtime < PAGE_RECHECK_S:
                return _read(target).strip()
        except OSError:
            pass
        address, why = publish_page(self.gs, self.run, self.root, tried=self.cfg.relay_path / PAGE_TRIED)
        if address:
            _write_keep(target, address + "\n")
        else:
            target.unlink(missing_ok=True)
            self.problems.append(f"the page has no address: {why}")
        return address

    def _assistant_root(self):
        """Where the assistant's folder is on this host, looked up again every ten minutes (Docker asks the
        container)."""
        if not self.gs.docker:
            return assistant_root(self.gs, self.run)
        target = self.cfg.relay_path / CHAT_ROOT_FILE
        try:
            if time.time() - target.stat().st_mtime < CHAT_ROOT_RECHECK_S:
                doc = json.loads(target.read_text(encoding="utf-8"))
                return Path(doc["base"]), list(doc["parts"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        found = assistant_root(self.gs, self.run)
        if found:
            try:
                _write_keep(target, json.dumps({"base": str(found[0]), "parts": found[1]}))
            except OSError as exc:
                log.warning("the assistant's folder was not kept: %s", exc)
        return found

    def chat_door(self):
        """Answer what Godspeed's assistant asked through its crypto-bot folder (chat_door.py)."""
        if self.config_path is None:
            return None
        try:
            root = self._assistant_root()
            if root is None:
                self.problems.append("the chat door is closed: Godspeed's assistant's folder was not found (is "
                                     "Godspeed running?)")
                return None
            listed = {}

            def perps():
                if "perps" not in listed:
                    if self.perps is not None:
                        listed["perps"] = self.perps()
                    else:
                        from .venue.hyperliquid import HyperliquidMarketData

                        listed["perps"] = HyperliquidMarketData(self.cfg.network).listed_perps()
                return listed["perps"]

            door = chat_door.Door(self.cfg, self.config_path, root, perps,
                                  backtest=self.backtest or (lambda c, n, s, e, o: chat_door.run_backtest(
                                      c, n, s, e, o, run=self.run)))
            out = door()
        except Exception as exc:  # noqa: BLE001 - the relay's other work must go on
            self.problems.append(f"the chat door failed: {type(exc).__name__}: {exc}")
            return None
        if out.get("problem"):
            self.problems.append(f"the chat door: {out['problem']}")
        return out.get("answered")

    def __call__(self):
        out = {"sent": self.deliver_alerts()}
        out["channel"] = self.check_channel()
        if not out["channel"] and not any(NO_CHANNEL in p for p in self.problems):
            self.problems.append(f"messages cannot reach you: {NO_CHANNEL}")
        out["deadline"] = self.sync_deadline()
        out["page"] = self.publish_page()
        out["chat"] = self.chat_door()
        out["waiting"] = len(outbox.waiting_alerts(self.cfg))
        out["problems"] = self.problems
        out["at"] = time.time()
        try:
            _write_keep(self.cfg.relay_path / STATUS_FILE, json.dumps(out))
        except OSError as exc:
            log.warning("relay status not written: %s", exc)
        return out
