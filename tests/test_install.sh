#!/usr/bin/env bash
# The installer, end to end, on a THROWAWAY Linux machine with systemd (a container made for this and deleted
# afterwards; never a real server). Run as root inside it:
#
#   bash tests/test_install.sh <a .tar.gz of this repository, e.g. from: git archive --format=tar.gz HEAD>
#
# It needs the internet (uv, PyPI, Hyperliquid's public market data) and changes the machine: it installs the
# add-on, updates it, removes it and purges it, and expects to find none of it there at the start.
# What it proves: each refusal gives its one sentence and changes nothing; a fresh install ends in a finished
# paper run on its timers; running it again keeps state, config, key, HALT and the page; uninstall keeps state
# and config; purge removes them; no key is made and nothing is switched to live.
set -uo pipefail

# Unattended, as the nightly scenario runs it (.github/workflows/install-scenario.yml): no terminal, so nothing can
# wait for an answer, and a time limit, so nothing runs forever. Neither skips or weakens a check: a test that is
# stopped by the limit fails, and the exit code says so. The script runs as its own process group so that the limit
# stops everything it started (a hung download, a hung installer), not only the script.
# (setsid --wait does not pass on a kill, so the watchdog leaves a marker file and the outer script exits 124.)
if [ -z "${TEST_INSTALL_OWN_GROUP:-}" ] && command -v setsid >/dev/null 2>&1; then
  mark="$(mktemp -u)"
  TEST_INSTALL_OWN_GROUP=1 TEST_INSTALL_TIMEOUT_MARK="$mark" setsid --wait bash "$0" "$@"; code=$?
  if [ -e "$mark" ]; then rm -f "$mark"; exit 124; fi
  exit "$code"
fi
exec < /dev/null
export DEBIAN_FRONTEND=noninteractive
LIMIT_SECONDS="${TEST_INSTALL_LIMIT_SECONDS:-1800}"
(
  trap '' TERM
  end=$((SECONDS + LIMIT_SECONDS))
  while kill -0 $$ 2>/dev/null; do
    if [ "$SECONDS" -ge "$end" ]; then
      echo "FAIL  the test ran longer than $LIMIT_SECONDS seconds and was stopped"
      [ -n "${TEST_INSTALL_TIMEOUT_MARK:-}" ] && : > "$TEST_INSTALL_TIMEOUT_MARK"
      kill -TERM -- "-$$" 2>/dev/null
      sleep 5
      kill -KILL -- "-$$" 2>/dev/null
      exit 0
    fi
    sleep 1
  done
) &

SRC="${1:?usage: test_install.sh <repository .tar.gz>}"
NAME="godspeed-crypto-bot"
STATE="/var/lib/$NAME"; ETC="/etc/$NAME"; CODE="/opt/$NAME"; PAGE="/var/lib/$NAME-page"
FAIL=0
pass() { printf 'ok    %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAIL=1; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
absent() { [ ! -e "$1" ]; }
nothing_installed() { absent "$CODE" && absent "$ETC" && ! id "$NAME" >/dev/null 2>&1 && ! ls /etc/systemd/system/$NAME-* >/dev/null 2>&1 && absent "/usr/local/sbin/$NAME"; }

WORK="$(mktemp -d)"; chmod 0755 "$WORK"
tar -xzf "$SRC" -C "$WORK" || { echo "cannot unpack $SRC"; exit 2; }
INSTALL="$WORK/install.sh"
[ -f "$INSTALL" ] || { echo "no install.sh at the top of $SRC"; exit 2; }
chmod 0755 "$INSTALL"

# A stand-in Godspeed folder: what a native installation leaves that the installer reads.
fake_godspeed() {  # fake_godspeed <dir> <version>
  local ref="0123456789abcdef0123456789abcdef01234567"
  mkdir -p "$1/.godspeed/integrated-runtime/versions/$ref/notebook"
  printf '%s\n' "$ref" > "$1/.godspeed/integrated-runtime/version"
  printf '{"name":"notebook","version":"%s"}\n' "$2" > "$1/.godspeed/integrated-runtime/versions/$ref/notebook/package.json"
}
GS="$WORK/godspeed"; fake_godspeed "$GS" "2.0.0-beta.9"
mkdir -p "$GS/skills/some-recipe"; echo "x" > "$GS/skills/some-recipe/SKILL.md"   # Godspeed already has a recipe room

refuses() {  # refuses <what> <sentence fragment> <command...>
  local what="$1" frag="$2"; shift 2
  local out; out="$("$@" 2>&1)"; local code=$?
  if [ "$code" -ne 0 ] && printf '%s' "$out" | grep -qF "$frag" && [ "$(printf '%s\n' "$out" | wc -l)" -le 2 ] && nothing_installed; then
    pass "refuses: $what"
  else fail "refuses: $what (exit $code): $out"; fi
}

echo "== refusals"
refuses "not root" "as root" su -s /bin/bash nobody -c "bash '$INSTALL' --godspeed '$GS' --source '$WORK/none'"
refuses "no --godspeed" "add --godspeed" bash "$INSTALL" --source "$WORK/none"
refuses "no Godspeed at the folder" "There is no Godspeed Mission Control at $WORK/nowhere" bash "$INSTALL" --godspeed "$WORK/nowhere"
mkdir -p "$WORK/empty"
refuses "a folder that is not Godspeed" "There is no Godspeed Mission Control at $WORK/empty" bash "$INSTALL" --godspeed "$WORK/empty"
fake_godspeed "$WORK/old" "2.0.0-beta.5"
refuses "a Godspeed older than the minimum" "is older than the 2.0.0-beta.6" bash "$INSTALL" --godspeed "$WORK/old" --source "$WORK/none"
fake_godspeed "$WORK/older" "1.9.0"
refuses "a much older Godspeed" "is older than" bash "$INSTALL" --godspeed "$WORK/older" --source "$WORK/none"
mkdir -p "$WORK/silent/.godspeed"
refuses "a Godspeed that does not tell its version" "did not tell its version" bash "$INSTALL" --godspeed "$WORK/silent" --source "$WORK/none"
refuses "an option it does not know" "Unknown option" bash "$INSTALL" --nonsense
mkdir -p "$WORK/fakebin"; printf '#!/bin/sh
echo Darwin
' > "$WORK/fakebin/uname"; chmod +x "$WORK/fakebin/uname"
refuses "not Linux" "Linux server only" env PATH="$WORK/fakebin:$PATH" bash "$INSTALL" --godspeed "$GS" --source "$WORK/none"
refuses "no systemd" "does not run systemd" unshare -m -- bash -c 'mount -t tmpfs none /run/systemd && exec bash "$0" "$@"' "$INSTALL" --godspeed "$GS" --source "$WORK/none"
mkdir -p "$WORK/odd&name/.godspeed"
refuses "a folder name it cannot write safely into the settings" "cannot write safely" bash "$INSTALL" --godspeed "$WORK/odd&name" --source "$WORK/none"
refuses "the country question unanswered, no terminal" "must answer" setsid --wait bash "$INSTALL" --godspeed "$GS" --source "$SRC" < /dev/null
# A "yes" is recorded with its date and stops the setup; nothing else of it is installed, and it is not asked again.
out="$(bash "$INSTALL" --godspeed "$GS" --restricted yes --source "$SRC" 2>&1)"; code=$?
[ "$code" -ne 0 ] && printf '%s' "$out" | grep -qF "cannot be set up for you" && pass "refuses: a person in a restricted place" || fail "restricted yes: exit $code: $out"
check "the yes answer is recorded with its date" grep -q '"restricted": true, "answered_on": "20' "$ETC/eligibility.json"
check "and nothing else of it is installed" bash -c "[ ! -e '$CODE' ] && ! id '$NAME' >/dev/null 2>&1 && ! ls /etc/systemd/system/$NAME-* >/dev/null 2>&1"
out="$(setsid --wait bash "$INSTALL" --godspeed "$GS" --source "$SRC" < /dev/null 2>&1)"; code=$?
[ "$code" -ne 0 ] && printf '%s' "$out" | grep -qF "cannot be set up for you" && pass "refuses again without being asked, from the recorded answer" || fail "recorded yes: exit $code: $out"
out="$(bash "$INSTALL" --godspeed "$GS" --restricted YES --source "$SRC" 2>&1)"; code=$?
[ "$code" -ne 0 ] && printf '%s' "$out" | grep -qF "cannot be set up for you" && pass "refuses: a restricted place, spelled out" || fail "restricted YES: exit $code"
rm -rf "$ETC"   # forget that answer: the person says no below, and the install goes ahead

echo "== the minimum version, compared as Godspeed's releases are named"
while read -r have min want; do
  got="$(CRYPTO_BOT_VERSION_OK_SELFTEST="$have $min" bash "$INSTALL" --godspeed "$GS" 2>&1)"
  [ "$got" = "$want" ] && pass "$have >= $min is $want" || fail "$have >= $min should be $want, was: $got"
done <<'TABLE'
2.0.0-beta.6 2.0.0-beta.6 yes
2.0.0-beta.10 2.0.0-beta.6 yes
2.0.0-beta.5 2.0.0-beta.6 no
2.0.0 2.0.0-beta.6 yes
2.0.0-rc.1 2.0.0-beta.6 yes
2.1.0 2.0.0-beta.6 yes
1.9.9 2.0.0-beta.6 no
3.0.0-beta.1 2.0.0-beta.6 yes
nonsense 2.0.0-beta.6 no
TABLE
check "the refusals and the table changed nothing" nothing_installed

echo "== a fresh install"
out="$(bash "$INSTALL" --godspeed "$GS" --restricted no --source "$SRC" 2>&1)"; code=$?
printf '%s\n' "$out" | tail -n 6
[ "$code" -eq 0 ] && pass "install finished" || fail "install failed (exit $code)"
check "the installer printed sentences only, no data block" bash -c '! printf "%s" "$1" | grep -q "[{}]"' _ "$out"
check "the data it left out is in the install log" grep -q '"ready": true' "$ETC/install.log"
check "the installer left no download folder behind" bash -c "! ls -d ${TMPDIR:-/tmp}/$NAME-* >/dev/null 2>&1"
check "program, user and command are there" test -x "$CODE/.venv/bin/python" -a -x "/usr/local/sbin/$NAME"
id "$NAME" >/dev/null 2>&1 && pass "system user exists" || fail "system user missing"
for k in daily guard deadline report terms relay; do
  check "timer $k is enabled and active" systemctl is-active --quiet "$NAME-$k.timer"
done
check "config starts in paper, mainnet, mix" grep -q '^mode = "paper"' "$ETC/config.toml"
grep -q '^network = "mainnet"' "$ETC/config.toml" && grep -q '^strategy = "mix"' "$ETC/config.toml" && pass "network and strategy" || fail "network/strategy"
check "config names the Godspeed folder" grep -q "^godspeed_dir = \"$GS\"" "$ETC/config.toml"
check "no live cap is written for the person (paper default only as a comment)" bash -c "! grep -q '^capital_cap_usdc' $ETC/config.toml && grep -q '^# capital_cap_usdc = 2000' $ETC/config.toml"
check "the country answer is kept with its date" grep -q '"restricted": false, "answered_on": "20' "$ETC/eligibility.json"
check "the recipe the assistant follows is installed, marked" test -f "$GS/skills/crypto-bot/SKILL.md" -a -f "$GS/skills/crypto-bot/.installed-by-$NAME"
check "the instructions for assistants are in the program folder" test -f "$CODE/INSTALL-FOR-ASSISTANTS.md"
status="$("/usr/local/sbin/$NAME" status 2>&1)"
printf '%s' "$status" | grep -q '"mode": "paper"' && pass "status says paper" || fail "status: $status"
printf '%s' "$status" | grep -q '"status": "complete"' && pass "one paper run finished" || fail "no finished run in status"
check "the page was written" test -s "$PAGE/index.html"
check "no key was made" bash -c "! ls /etc/credstore.encrypted/$NAME.agent-key.* >/dev/null 2>&1"
check "nothing live was confirmed" bash -c "! ls $ETC/LIVE-CONFIRMED* >/dev/null 2>&1"
systemd-analyze verify /etc/systemd/system/$NAME-*.service >/dev/null 2>&1 && pass "units verify" || fail "units do not verify"
grep -q "ImportCredential=$NAME" /etc/systemd/system/$NAME-daily.service && grep -q "ReadWritePaths=$STATE" /etc/systemd/system/$NAME-daily.service \
  && grep -q "^User=$NAME" /etc/systemd/system/$NAME-daily.service && pass "hardening kept: own user, ReadWritePaths, ImportCredential" || fail "hardening lost"
systemctl cat "$NAME-relay.service" | grep -q "ExecStart=.*cli relay" && pass "relay unit runs the relay" || fail "relay unit"

echo "== running it again is the update"
sed -i 's/^strategy = "mix"/strategy = "trend"/' "$ETC/config.toml"
echo "kept" > "$STATE/keepme"; chown "$NAME:$NAME" "$STATE/keepme"
echo "halted for the test" > "$STATE/HALT"
printf 'not a real key' | systemd-creds encrypt --name="$NAME.agent-key.mainnet" - "/etc/credstore.encrypted/$NAME.agent-key.mainnet"  # a real credential, as keygen makes
key_before="$(sha256sum "/etc/credstore.encrypted/$NAME.agent-key.mainnet" | cut -d' ' -f1)"
echo "my page" > "$PAGE/extra.html"
out="$(bash "$INSTALL" --godspeed "$GS" --restricted no --source "$SRC" 2>&1)"; code=$?
printf '%s\n' "$out" | tail -n 3
[ "$code" -eq 0 ] && pass "the update finished" || fail "the update failed (exit $code)"
check "config kept (strategy trend)" grep -q '^strategy = "trend"' "$ETC/config.toml"
check "state kept" test "$(cat "$STATE/keepme")" = kept
check "records kept" test -s "$STATE/bot.sqlite"
check "HALT kept" test -f "$STATE/HALT"
check "the update says it is an update, and that trading is on hold" bash -c 'printf "%s" "$1" | grep -q "is updated and runs in paper mode" && printf "%s" "$1" | grep -q "Trading is on hold"' _ "$out"
check "key kept" test "$(sha256sum /etc/credstore.encrypted/$NAME.agent-key.mainnet | cut -d' ' -f1)" = "$key_before"
check "the update left no download folder behind either" bash -c "! ls -d ${TMPDIR:-/tmp}/$NAME-* >/dev/null 2>&1"
check "page kept" test "$(cat "$PAGE/extra.html")" = "my page"
check "state still belongs to the bot's user" test "$(stat -c %U "$STATE/keepme")" = "$NAME"
for k in daily guard relay; do check "timer $k is active after the update" systemctl is-active --quiet "$NAME-$k.timer"; done
rm -f "$STATE/HALT"

# Since 0.2.0 a live bot is updated all the same (an update from 0.1 must reach a live bot, whose go-live sentence now
# names the strategy): its own check fails, real money stays paused until it passes, and nothing is run.
echo "== an update of a configuration that says live (however it is spelled) only checks, never runs"
cp "$ETC/config.toml" "$WORK/config.paper"
sed -i "s/^mode = .*/  mode = 'live'/" "$ETC/config.toml"
runs_before="$(sqlite3 "$STATE/bot.sqlite" 'select count(*) from runs' 2>/dev/null || echo na)"
out="$(bash "$INSTALL" --godspeed "$GS" --source "$SRC" 2>&1)"; code=$?
[ "$code" -eq 0 ] && printf '%s' "$out" | grep -q "own check (preflight) did not pass" \
  && printf '%s' "$out" | grep -q "real money is paused until its own check above passes" \
  && pass "a live-looking update is made, says its own check failed (no key here) and that real money is paused" \
  || fail "live-looking update: exit $code: $(printf '%s' "$out" | tail -n 2)"
check "its refusal lists the problems as sentences, not data" bash -c '! printf "%s" "$1" | grep -q "[{}]" && printf "%s" "$1" | grep -q "^  - "' _ "$out"
runs_after="$(sqlite3 "$STATE/bot.sqlite" 'select count(*) from runs' 2>/dev/null || echo na)"
[ "$runs_before" = "$runs_after" ] && pass "and no run was made" || fail "a run was made during a live-looking update"
cp "$WORK/config.paper" "$ETC/config.toml"

echo "== uninstall"
out="$("/usr/local/sbin/$NAME" uninstall 2>&1)"; code=$?
printf '%s\n' "$out"
[ "$code" -eq 0 ] && pass "uninstall finished" || fail "uninstall failed (exit $code)"
check "program gone" absent "$CODE"
check "the recipe is gone, the person's other recipes stay" bash -c "[ ! -e '$GS/skills/crypto-bot' ] && [ -f '$GS/skills/some-recipe/SKILL.md' ]"
check "command gone" absent "/usr/local/sbin/$NAME"
check "units gone" bash -c "! ls /etc/systemd/system/$NAME-* >/dev/null 2>&1"
check "user gone" bash -c "! id $NAME >/dev/null 2>&1"
check "nothing of it runs" bash -c "! systemctl list-timers --all | grep -q $NAME"
check "state kept" test -s "$STATE/bot.sqlite"
check "config kept" test -f "$ETC/config.toml"
check "key kept" test -f "/etc/credstore.encrypted/$NAME.agent-key.mainnet"
printf '%s' "$out" | grep -q "positions stay open" && pass "says that positions stay open" || fail "no sentence about open positions"

echo "== install again over the kept state, then purge"
out="$(bash "$INSTALL" --godspeed "$GS" --restricted no --source "$SRC" 2>&1)"; code=$?
[ "$code" -eq 0 ] && pass "reinstall over kept state" || { fail "reinstall failed (exit $code)"; printf '%s\n' "$out" | tail -n 5; }
check "kept config used again" grep -q '^strategy = "trend"' "$ETC/config.toml"
out="$("/usr/local/sbin/$NAME" uninstall --purge 2>&1)"; code=$?
[ "$code" -eq 0 ] && pass "purge finished" || fail "purge failed (exit $code)"
check "state gone" absent "$STATE"
check "config gone" absent "$ETC"
check "page gone" absent "$PAGE"
check "relay files gone" absent "$STATE-relay"
check "encrypted key gone" bash -c "! ls /etc/credstore.encrypted/$NAME.agent-key.* >/dev/null 2>&1"
check "nothing of it is left" nothing_installed

echo "== a recipe the person wrote with the same name is left alone"
GS2="$WORK/godspeed-own"; fake_godspeed "$GS2" "2.0.0"
mkdir -p "$GS2/skills/crypto-bot"; echo "# my own recipe" > "$GS2/skills/crypto-bot/SKILL.md"
out="$(bash "$INSTALL" --godspeed "$GS2" --restricted no --source "$SRC" 2>&1)"; code=$?
[ "$code" -eq 0 ] && pass "install beside the person's own recipe" || { fail "install failed (exit $code)"; printf '%s
' "$out" | tail -n 5; }
check "their recipe is untouched" test "$(cat "$GS2/skills/crypto-bot/SKILL.md")" = "# my own recipe"
printf '%s' "$out" | grep -q "Left alone" && pass "says it left it alone" || fail "no 'Left alone' sentence"
"/usr/local/sbin/$NAME" uninstall --purge >/dev/null 2>&1
check "their recipe survives the uninstall" test "$(cat "$GS2/skills/crypto-bot/SKILL.md")" = "# my own recipe"
check "nothing of it is left" nothing_installed

rm -rf "$WORK"
if [ "$FAIL" -eq 0 ]; then echo "ALL PASSED"; else echo "SOME FAILED"; fi
exit "$FAIL"
