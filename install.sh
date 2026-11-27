#!/usr/bin/env bash
# Crypto Bot, an add-on for Godspeed Mission Control: the installer for a Linux server with systemd.
#
#   curl -fsSL https://raw.githubusercontent.com/MichaelZelbel/godspeed-crypto-bot/v0.2.1/install.sh | bash -s -- --godspeed <your Godspeed Mission Control folder>
#
# Run it as root. It installs the program, a system user, the timers and a configuration that starts in PAPER
# mode (simulated fills on the live order book, nothing signed, 2,000 USDC pretend money), then checks itself
# and does one paper run. It creates no key and never switches to real money.
#
# Safe to run again: running it again is the update. The configuration, key, records, HALT file and page stay.
# To take it out: godspeed-crypto-bot uninstall   (add --purge to remove the records and settings as well)
#
# Options:
#   --godspeed DIR   the Godspeed Mission Control folder (required)
#   --restricted no|yes   the country question, answered by the person: do they live in the United States or
#                    Ontario, or are they a resident or citizen of a sanctioned country? Asked once (the answer is
#                    kept with its date); "yes" stops the setup. Without it, the question is asked on the terminal.
#   --source PATH    install from a local checkout or a .tar.gz of one instead of downloading the tagged release
#                    (also CRYPTO_BOT_SOURCE)
#
# The name of the add-on is NAME below and in godspeed_crypto_bot/names.py, and nowhere else.

main() {
  set -euo pipefail

  # ---- names and constants
  local NAME="godspeed-crypto-bot"
  local DISPLAY="Crypto Bot"
  local REPO="MichaelZelbel/godspeed-crypto-bot"
  local TAG="v0.2.1"                       # the release this install.sh belongs to: the install line names it
  local MINIMUM_GODSPEED="2.0.0-beta.6"    # the oldest Godspeed with the add-on deadline door on both server installs
  local CODE="/opt/$NAME" STATE="/var/lib/$NAME" ETC="/etc/$NAME"
  local PAGE="/var/lib/$NAME-page" RELAY="/var/lib/$NAME-relay"

  say() { printf '%s\n' "$*"; }
  die() { say "$*" >&2; exit 1; }

  # ---- arguments
  local godspeed="" source="${CRYPTO_BOT_SOURCE:-}" restricted=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --godspeed) godspeed="${2:-}"; shift 2 || die "--godspeed needs the Godspeed Mission Control folder." ;;
      --godspeed=*) godspeed="${1#*=}"; shift ;;
      --restricted) restricted="${2:-}"; shift 2 || die "--restricted needs yes or no." ;;
      --restricted=*) restricted="${1#*=}"; shift ;;
      --source) source="${2:-}"; shift 2 || die "--source needs a folder or a .tar.gz." ;;
      --source=*) source="${1#*=}"; shift ;;
      *) die "Unknown option $1. The options are --godspeed <folder>, --restricted no|yes and --source <path>." ;;
    esac
  done

  # ---- refusals: each in one plain sentence, each before anything is changed
  [ "$(uname -s)" = "Linux" ] || die "$DISPLAY installs on a Linux server only: it must run every day at 00:02 UTC and check every five minutes, so it needs a server that stays on."
  [ -d /run/systemd/system ] || die "This server does not run systemd, which $DISPLAY needs for its daily timers."
  [ "$(id -u)" -eq 0 ] || die "Run this install line as root (for example with sudo)."
  [ -n "$godspeed" ] || die "Say where Godspeed Mission Control is installed: add --godspeed <its folder> to the install line."
  [ -d "$godspeed" ] && [ -d "$godspeed/.godspeed" ] || die "There is no Godspeed Mission Control at $godspeed; install Godspeed first, then run this line again."
  godspeed="$(cd "$godspeed" && pwd)"
  case "$godspeed" in
    *[!A-Za-z0-9._/@+:=,~\ -]*) die "The folder name $godspeed has a character the installer cannot write safely into its settings; use a folder with only letters, digits and . _ / @ + : = , ~ - (and spaces), then run this line again." ;;
  esac

  # Godspeed's version, from its files (a native install) or by asking it (the Docker install asks inside its
  # container), then the comparison in awk: a release is newer than its own betas, and betas compare by number.
  gs_version() {
    local ref pkg v
    if [ -r "$godspeed/.godspeed/integrated-runtime/version" ]; then
      ref="$(tr -d '[:space:]' < "$godspeed/.godspeed/integrated-runtime/version")"
      pkg="$godspeed/.godspeed/integrated-runtime/versions/$ref/notebook/package.json"
      if [ -r "$pkg" ]; then
        grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$pkg" | head -n 1 | sed 's/.*"\([^"]*\)"$/\1/'
        return 0
      fi
    fi
    if [ -r "$godspeed/.godspeed/model-host.json" ] && [ -f "$godspeed/compose.yaml" ] && command -v docker >/dev/null 2>&1; then
      v="$(docker compose -f "$godspeed/compose.yaml" --project-directory "$godspeed" exec -T --user hermes godspeed \
           node -e "fetch('http://127.0.0.1:47832/health').then(r=>r.json()).then(j=>console.log(j.version||''))" 2>/dev/null || true)"
      [ -n "$v" ] && { printf '%s\n' "$v"; return 0; }
    fi
    curl -fsS --max-time 10 http://127.0.0.1:47831/health 2>/dev/null | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
  }
  version_ok() {  # version_ok <have> <minimum>: success when have >= minimum
    awk -v a="$1" -v b="$2" '
      function parse(v, out,   m, rest, n, i, parts) {
        sub(/^v/, "", v); sub(/\+.*/, "", v)
        out["pre"] = ""
        if (index(v, "-")) { out["pre"] = substr(v, index(v, "-") + 1); v = substr(v, 1, index(v, "-") - 1) }
        n = split(v, parts, ".")
        if (n != 3 || parts[1] !~ /^[0-9]+$/ || parts[2] !~ /^[0-9]+$/ || parts[3] !~ /^[0-9]+$/) return 0
        out[1] = parts[1] + 0; out[2] = parts[2] + 0; out[3] = parts[3] + 0
        return 1
      }
      function cmp_pre(x, y,   xs, ys, nx, ny, i, k) {
        if (x == "" && y == "") return 0
        if (x == "") return 1          # a release sorts after its own prereleases
        if (y == "") return -1
        nx = split(x, xs, "."); ny = split(y, ys, ".")
        k = (nx > ny) ? nx : ny
        for (i = 1; i <= k; i++) {
          if (i > nx) return -1
          if (i > ny) return 1
          if (xs[i] ~ /^[0-9]+$/ && ys[i] ~ /^[0-9]+$/) { if (xs[i] + 0 != ys[i] + 0) return (xs[i] + 0 < ys[i] + 0) ? -1 : 1 }
          else if (xs[i] != ys[i]) return (xs[i] < ys[i]) ? -1 : 1
        }
        return 0
      }
      BEGIN {
        if (!parse(a, A) || !parse(b, B)) exit 1
        for (i = 1; i <= 3; i++) if (A[i] != B[i]) exit (A[i] > B[i]) ? 0 : 1
        exit (cmp_pre(A["pre"], B["pre"]) >= 0) ? 0 : 1
      }'
  }
  if [ -n "${CRYPTO_BOT_VERSION_OK_SELFTEST:-}" ]; then  # tests: print whether "<have> <minimum>" passes
    version_ok "${CRYPTO_BOT_VERSION_OK_SELFTEST%% *}" "${CRYPTO_BOT_VERSION_OK_SELFTEST##* }" && say yes || say no
    return 0
  fi
  local have
  have="$(gs_version || true)"
  [ -n "$have" ] || die "Godspeed Mission Control at $godspeed did not tell its version; start it, then run this line again."
  version_ok "$have" "$MINIMUM_GODSPEED" || die "Godspeed Mission Control $have is older than the $MINIMUM_GODSPEED that $DISPLAY needs; update Godspeed, then run this line again."

  # ---- the country question: asked once, answered by the person (their assistant asks them and passes --restricted)
  local QUESTION="Do you live in the United States or Ontario (Canada), or are you a resident or citizen of a country under international sanctions?"
  local REFUSAL="Hyperliquid does not allow people who live in the United States or Ontario, or who are residents or citizens of a sanctioned country, to use it, so $DISPLAY cannot be set up for you."
  write_answer() {  # write_answer true|false: kept with its date, so it is asked once
    install -d -m 0755 "$ETC"
    echo "{ \"restricted\": $1, \"answered_on\": \"$(date -u +%F)\", \"question\": \"$QUESTION\" }" > "$ETC/eligibility.json"
    chmod 0644 "$ETC/eligibility.json"
  }
  local recorded="" answered_now=""
  if [ -f "$ETC/eligibility.json" ]; then
    if grep -q '"restricted": *true' "$ETC/eligibility.json"; then recorded="yes"
    elif grep -q '"restricted": *false' "$ETC/eligibility.json"; then recorded="no"
    else recorded="unreadable"; fi
  fi
  if [ -z "$restricted" ]; then
    case "$recorded" in
      no) ;;  # answered before: not asked again
      yes) die "$REFUSAL (You answered this on a day recorded in $ETC/eligibility.json; if that was a mistake, run the line again with --restricted no.)" ;;
      *)
        if [ -r /dev/tty ] && [ -w /dev/tty ] && ( : < /dev/tty ) 2>/dev/null; then
          printf '%s (yes/no) ' "$QUESTION" > /dev/tty
          read -r restricted < /dev/tty || restricted=""
        fi
        ;;
    esac
  fi
  if [ -n "$restricted" ] || [ "$recorded" != "no" ]; then
    case "$restricted" in
      [Yy]|[Yy][Ee][Ss]) write_answer true; die "$REFUSAL" ;;
      [Nn]|[Nn][Oo]) answered_now="yes" ;;
      *) die "Before installing, the person must answer: $QUESTION Add --restricted no (or --restricted yes) to the install line." ;;
    esac
  fi

  # ---- the program: a local source, or the tagged release
  # The download folder is removed when the installer ends, however it ends. Its name is kept in a variable the EXIT
  # trap can still read after main() has returned (a local one is gone by then, and the folder stayed behind).
  local src
  CRYPTO_BOT_WORK="$(mktemp -d "${TMPDIR:-/tmp}/$NAME-XXXXXX")"
  local work="$CRYPTO_BOT_WORK"
  trap 'rm -rf "${CRYPTO_BOT_WORK:-}"' EXIT
  if [ -n "$source" ]; then
    if [ -d "$source" ]; then src="$(cd "$source" && pwd)"
    elif [ -f "$source" ]; then tar -xzf "$source" -C "$work" || die "Could not unpack $source."; src="$(find "$work" -maxdepth 2 -name pyproject.toml -printf '%h\n' | head -n 1)"
    else die "There is no source at $source."; fi
  else
    command -v curl >/dev/null 2>&1 || die "curl is needed to download $DISPLAY; install it, then run this line again."
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG" | tar -xz -C "$work" 2>/dev/null \
      || die "Could not download $DISPLAY $TAG from GitHub. Check the internet connection and run this line again."
    src="$(find "$work" -maxdepth 2 -name pyproject.toml -printf '%h\n' | head -n 1)"
  fi
  [ -n "${src:-}" ] && [ -f "$src/pyproject.toml" ] && [ -f "$src/deploy/requirements.txt" ] && [ -d "$src/godspeed_crypto_bot" ] \
    || die "That source is not $DISPLAY (no pyproject.toml, deploy/requirements.txt or godspeed_crypto_bot there)."
  say "$DISPLAY $TAG for Godspeed Mission Control $have at $godspeed"

  # ---- uv (the program's own Python and packages), the user, the folders
  if ! command -v uv >/dev/null 2>&1 && [ ! -x /usr/local/bin/uv ]; then
    command -v curl >/dev/null 2>&1 || die "curl is needed to install uv; install it, then run this line again."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 \
      || die "Could not install uv (https://docs.astral.sh/uv/). Check the internet connection and run this line again."
  fi
  local UV; UV="$(command -v uv || echo /usr/local/bin/uv)"

  if ! id "$NAME" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE" --shell /usr/sbin/nologin "$NAME"
  fi
  # Stop the timers while the files change (a run already started finishes on the files it loaded).
  local k
  for k in daily guard deadline report terms relay; do systemctl stop "$NAME-$k.timer" 2>/dev/null || true; done
  # A run that is going on finishes (up to five minutes) before its files change; one that is still going then is
  # stopped (an interrupted daily run resumes with the decision it saved).
  local busy tries
  for tries in $(seq 1 60); do
    busy=0
    for k in daily guard deadline report terms relay; do systemctl is-active --quiet "$NAME-$k.service" && busy=1; done
    [ "$busy" -eq 0 ] && break
    sleep 5
  done
  for k in daily guard deadline report terms relay; do systemctl stop "$NAME-$k.service" 2>/dev/null || true; done
  # An attempt to give the page its address that Caddy did not take is never repeated by the relay on its own;
  # running the install line again is the one way to try it again.
  rm -f "$RELAY/page-tried.json"

  install -d -m 0755 "$CODE" "$ETC" "$ETC/strategies" "$RELAY"
  install -d -m 0700 -o "$NAME" -g "$NAME" "$STATE"
  install -d -m 0755 -o "$NAME" -g "$NAME" "$PAGE"
  install -d -m 0700 /etc/credstore.encrypted
  chown -R "$NAME:$NAME" "$STATE" "$PAGE"   # a reinstall may have made a new user with the same name

  rm -rf "$CODE/godspeed_crypto_bot" "$CODE/deploy"
  rm -rf "$CODE/skill"
  (cd "$src" && tar -cf - godspeed_crypto_bot deploy skill docs pyproject.toml uv.lock README.md INSTALL-FOR-ASSISTANTS.md $([ -f LICENSE ] && echo LICENSE) 2>/dev/null) | tar -xf - -C "$CODE"
  find "$CODE/godspeed_crypto_bot" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  export UV_PYTHON_INSTALL_DIR="$CODE/python"   # a Python it has to download must be readable by the bot's user
  "$UV" venv --quiet --allow-existing --python 3.12 "$CODE/.venv" \
    || die "Could not set up Python 3.12 for $DISPLAY. Check the internet connection and run this line again."
  "$UV" pip install --quiet --python "$CODE/.venv/bin/python" --require-hashes -r "$CODE/deploy/requirements.txt" \
    || die "Could not install $DISPLAY's packages. Check the internet connection and run this line again."
  chown -R root:root "$CODE"
  chmod -R go-w "$CODE"

  # ---- the units, rendered from the templates with the one name
  render() {
    sed -e "s|@NAME@|$NAME|g" -e "s|@DISPLAY@|$DISPLAY|g" -e "s|@CODE@|$CODE|g" -e "s|@STATE@|$STATE|g" -e "s|@PAGE@|$PAGE|g" "$1"
  }
  local f base ext
  for f in "$CODE"/deploy/templates/*.service "$CODE"/deploy/templates/*.timer; do
    base="$(basename "$f")"; ext="${base##*.}"; base="${base%.*}"
    render "$f" > "/etc/systemd/system/$NAME-$base.$ext"
    chmod 0644 "/etc/systemd/system/$NAME-$base.$ext"
  done
  render "$CODE/deploy/templates/wrapper" > "/usr/local/sbin/$NAME"
  chmod 0755 "/usr/local/sbin/$NAME"
  systemd-analyze verify /etc/systemd/system/$NAME-*.service /etc/systemd/system/$NAME-*.timer >/dev/null 2>&1 \
    || die "systemd refused the units $DISPLAY wrote; run: systemd-analyze verify /etc/systemd/system/$NAME-*.service"

  # ---- the configuration: written once, then only kept (a new Godspeed folder is the one thing it updates)
  local updating=no
  [ -f "$ETC/config.toml" ] && updating=yes
  if [ ! -f "$ETC/config.toml" ]; then
    sed -e 's|^mode = "dry-run".*|mode = "paper"                 # paper: simulated fills; real money only after you confirm it yourself|' \
        -e 's|^capital_cap_usdc = .*|# capital_cap_usdc = 2000    # paper: pretend money (2000 unless you set it). Live: you name the most the bot may use|' \
        "$CODE/deploy/config.example.toml" > "$ETC/config.toml"
    chmod 0644 "$ETC/config.toml"
  fi
  if grep -q '^godspeed_dir *=' "$ETC/config.toml"; then
    sed -i "s|^godspeed_dir *=.*|godspeed_dir = \"$godspeed\"|" "$ETC/config.toml"
  else
    sed -i "/^network *=/a godspeed_dir = \"$godspeed\"           # the Godspeed Mission Control folder" "$ETC/config.toml"
  fi

  [ -n "$answered_now" ] && write_answer false

  # ---- the chat door: a crypto-bot folder in the mission control folder Godspeed's assistant works in (its strategy
  # files, and the requests the relay answers) and the recipe it follows, where it reads recipes. On a native install
  # that is the Godspeed folder; on the one-line Docker server the folder inside Godspeed's container, found through
  # the container's mounts (chat_door.py). A recipe the owner wrote with the same name is left alone.
  local door_said door_ok=yes
  door_said="$("/usr/local/sbin/$NAME" chat-door --version "$TAG" 2>&1)" || door_ok=no

  systemctl daemon-reload
  for k in daily guard deadline report terms relay; do systemctl enable --now "$NAME-$k.timer" >/dev/null 2>&1 \
    || die "Could not start the $NAME-$k timer; see: systemctl status $NAME-$k.timer"; done

  # ---- check itself and do one paper run (never a live one: an update of a live bot only checks)
  local mode said_ok
  # The mode is read as TOML (any spelling of it), and anything but paper or dry-run counts as live: no paper run then.
  mode="$("$CODE/.venv/bin/python" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("mode", "dry-run"))' "$ETC/config.toml" 2>/dev/null || echo unknown)"
  case "$mode" in paper|dry-run) ;; *) mode="live" ;; esac
  # What the program prints as data goes to the install log; the person reads sentences only.
  local log="$ETC/install.log" said
  echo "== $(date -u '+%F %T') UTC: $DISPLAY $TAG, install or update" >> "$log"
  chmod 0644 "$log"
  printf 'chat door:\n%s\n' "$door_said" >> "$log"
  if said="$("/usr/local/sbin/$NAME" preflight 2>>"$log")"; then said_ok=yes; else said_ok=no; fi
  printf '%s\n' "$said" >> "$log"
  local live_paused=no
  if [ "$said_ok" != yes ]; then
    say "$DISPLAY's own check (preflight) did not pass:"
    printf '%s' "$said" | "$CODE/.venv/bin/python" -c 'import json, sys
try:
    for problem in json.load(sys.stdin).get("problems", []): print("  - " + problem)
except ValueError:
    pass'
    # A live bot is updated all the same: its real money stays off until the check passes (for an update from 0.1,
    # until the person types the new sentence, which names the strategy), and its messages say so.
    [ "$mode" = "live" ] || die "The details are in $log."
    live_paused=yes
  fi
  if [ "$mode" != "live" ]; then
    "/usr/local/sbin/$NAME" run-daily --allow-late >> "$log" 2>&1 \
      || die "$DISPLAY's first paper run did not finish; the details are in $log, and '$NAME run-daily --allow-late' shows why."
  fi
  "/usr/local/sbin/$NAME" relay >/dev/null 2>&1 || true
  local channel
  channel="$("$CODE/.venv/bin/python" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("channel"))' "$RELAY/relay-status.json" 2>/dev/null || echo unknown)"
  local address
  address="$("/usr/local/sbin/$NAME" status 2>/dev/null | sed -n 's/^ *"page": *"\([^"]*\)".*/\1/p' | head -n 1)"

  say ""
  if [ "$live_paused" = yes ]; then
    say "$DISPLAY is updated; its records, key and settings are as they were, and real money is paused until its own check above passes. Nobody else can type that sentence for you."
  elif [ "$mode" = "live" ] && [ -e "$STATE/HALT" ]; then
    say "$DISPLAY is updated; its records, key and settings are as they were. Trading is on hold (a HALT file is there: $(head -n 1 "$STATE/HALT")); run '$NAME resume' when you want it to trade again."
  elif [ "$mode" = "live" ]; then
    say "$DISPLAY is updated and still trading live; its records, key and settings are as they were."
  else
    if [ "$updating" = yes ]; then
      say "$DISPLAY is updated and runs in paper mode (simulated money on real prices); its records, key and settings are as they were."
    else
      say "$DISPLAY is installed and running in paper mode (simulated money on real prices); it decides every day at 00:02 UTC, and nothing real is used until you confirm it yourself."
    fi
    if [ -e "$STATE/HALT" ]; then
      say "Trading is on hold (a HALT file is there: $(head -n 1 "$STATE/HALT")); run '$NAME resume' when you want it to trade again."
    fi
  fi
  if [ "$door_ok" = yes ]; then
    printf '%s\n' "$door_said" | grep '^Left alone:' || true
    say "Godspeed's assistant has $DISPLAY's recipe and a crypto-bot folder, so it can bring your own strategy to the bot in the chat."
  else
    say "Godspeed's assistant could not be given $DISPLAY's recipe and folder yet ($(printf '%s' "$door_said" | tail -n 1 | sed "s/^$NAME: //")); everything else works, and running this line again tries it again."
  fi
  if [ -n "$address" ]; then say "Its page, behind your Godspeed sign-in: $address"
  else say "Its page gets an address as soon as Godspeed's web address is found; '$NAME status' shows it."; fi
  if [ "$channel" = "False" ]; then
    say "Its messages cannot reach you yet: your Godspeed has no Telegram connected. Connect Telegram in your Godspeed; messages wait until then. '$NAME test-alert' then queues a test message to check that one arrives."
  else
    say "'$NAME test-alert' queues one test message for your Godspeed's Telegram; check whether it arrives."
  fi
}

main "$@"
