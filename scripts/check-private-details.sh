#!/usr/bin/env bash
# Fails (exit 1, one line per finding) when the tree holds anything of a maintainer's own setup: a wallet address
# other than a documented burn address, a host name, server path or tool of theirs, an address inside a private
# network, or their name outside LICENSE.
# Usage: scripts/check-private-details.sh [root]     (default: the repository this script is in)
#
# The patterns below are written with character classes (like mic[h]ael) so that this file does not contain what it
# looks for. LICENSE is exempt from the name check only. The maintainer's own host names, paths and tools are NOT
# listed here (a public file would then publish them): they go in a file outside the tree, one extended regular
# expression per line, named by CRYPTO_BOT_PRIVATE_PATTERNS, which the maintainer runs this check with before a release.
set -uo pipefail
root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$root" || exit 2

# The files that belong to the tree: tracked ones in a git checkout, else everything but build leftovers.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && [ -n "$(git ls-files | head -n 1)" ]; then
  files() { git ls-files -z --cached --others --exclude-standard; }
else
  files() { find . -type f -not -path './.git/*' -not -path './.venv/*' -not -path '*/__pycache__/*' -not -path './python/*' -print0 | sed -z 's|^\./||'; }
fi

burn='^0x(0{40}|0{36}dead|dead0{36})$'
hosts='hstgr[.]cloud|srv[0-9]{6,}|mc-notif[y]|mc-artifact[s]|mc-crypto-bo[t]|godspeed-mission-control-crypt[o]|C:[\\/]godspeed|/mnt/[c]/'
# addresses inside a private network: 10.x, 172.16-31.x, 192.168.x and the shared 100.64-127.x range
nets='\b(10[.][0-9]+[.][0-9]+[.][0-9]+|192[.]168[.][0-9]+[.][0-9]+|172[.](1[6-9]|2[0-9]|3[01])[.][0-9]+[.][0-9]+|100[.](6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])[.][0-9]+[.][0-9]+)\b'
names='mic[h]ael|zelbe[l]'
own_repo='Michae[l]Zelbe[l]/godspeed-crypto-bot'   # the public repository's own address is not a private detail

extra=""
if [ -n "${CRYPTO_BOT_PRIVATE_PATTERNS:-}" ] && [ -f "$CRYPTO_BOT_PRIVATE_PATTERNS" ]; then
  extra="$(grep -v '^[[:space:]]*$' "$CRYPTO_BOT_PRIVATE_PATTERNS" | paste -sd'|' -)"
fi
all="$hosts|$nets${extra:+|$extra}"

status=0
report() { printf '%s\n' "$1"; status=1; }

while IFS= read -r -d '' f; do
  licence=0
  [ "$f" = "LICENSE" ] && licence=1   # the copyright holder's name belongs here; nothing else does
  case "$f" in *.gz) text="$(gzip -dc "$f" 2>/dev/null | tr -d '\0')";; *) text="$(tr -d '\0' < "$f" 2>/dev/null)";; esac
  [ -n "$text" ] || continue
  while IFS= read -r addr; do
    low="$(printf '%s' "$addr" | tr 'A-F' 'a-f')"
    [[ "$low" =~ $burn ]] && continue
    report "$f: a wallet address ($addr)"
  done < <(printf '%s' "$text" | grep -oE '0x[0-9a-fA-F]{40}' | sort -u)
  if hit="$(printf '%s' "$text" | grep -oiE "$all" | sort -u | head -n 3)"; [ -n "$hit" ]; then
    report "$f: a private host, path or tool ($(printf '%s' "$hit" | tr '\n' ' '))"
  fi
  [ "$licence" -eq 1 ] && continue
  masked="$(printf '%s' "$text" | sed -E "s#$own_repo#REPO#g")"
  if hit="$(printf '%s' "$masked" | grep -oiE "$names" | sort -u | head -n 2)"; [ -n "$hit" ]; then
    report "$f: a name ($(printf '%s' "$hit" | tr '\n' ' '))"
  fi
done < <(files)
exit "$status"
