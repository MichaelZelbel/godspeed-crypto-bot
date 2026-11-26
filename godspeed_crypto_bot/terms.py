"""Weekly check that Hyperliquid's terms of use did not change.

The terms (app.hyperliquid.xyz/terms) exist only inside the app's JavaScript: the page itself is an
empty shell. The check loads the app's index, follows its main bundle to the chunk that holds the terms
(a JS file containing "Restricted Persons"), and extracts the "Last updated" date and the Restricted
Persons clause (the string literal that defines it).

Reviewed baseline (2026-10-13): "Last updated on June 15, 2026"; clause 1.6 names the United States,
Ontario (Canada) and sanctioned jurisdictions as Restricted Persons. Whether the OWNER may use Hyperliquid is
asked once at setup (eligibility.py); this check only watches the clause. If the date or the clause changes,
the bot stops opening new positions (it never flattens for this) and sends one alert quoting the new clause. If fetching or parsing fails three weeks in a row, it
alerts once.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from urllib.parse import urljoin

import requests

from .names import DISPLAY_NAME, NAME

TERMS_URL = "https://app.hyperliquid.xyz/terms"
BASELINE = {
    "date": "June 15, 2026",
    # sha256 of the reviewed clause 1.6 below, whitespace-normalized, UTF-8 (read 2026-10-13 from
    # assets/TermsOfUse-D17UCFbr.js)
    "sha256": "3a5445b0d2b3254eb3fe6409f43edfc8595c5a17ef51ddf938327c4e9c65e85d",
    "clause": (
        "1.6 The Interface is not available to “Restricted Persons.” For the purposes of these Terms, "
        "Restricted Persons include: (a) persons or entities who reside in, are located in, are incorporated in, or "
        "have a registered office in the United States of America or Ontario, Canada; (b) persons or entities who "
        "reside in, are located in, are incorporated in, or have a registered office in jurisdictions subject to "
        "applicable economic and trade sanctions or export control laws and regulations (collectively, "
        "“Restricted Territories”); and (c) citizens of Restricted Territories, regardless of their "
        "location. Restricted Persons are strictly prohibited from accessing or using the Interface described "
        "herein."
    ),
}
# The country question the exchange's terms make necessary, as it is put to the person, and what is said to a "yes".
# (The installer carries a copy of the words; eligibility.py keeps the answer.)
QUESTION = ("Do you live in the United States or Ontario (Canada), or are you a resident or citizen of a "
            "country under international sanctions?")
REFUSAL = ("Hyperliquid does not allow people who live in the United States or Ontario, or who are residents "
           f"or citizens of a sanctioned country, to use it, so {DISPLAY_NAME} cannot be set up for you.")
FAILURES_BEFORE_ALERT = 3
UA = {"User-Agent": f"Mozilla/5.0 (X11; Linux x86_64) {NAME} terms check"}
DATE_RE = re.compile(r"Last updated on ([A-Z][a-z]+ \d{1,2}, \d{4})")


class TermsError(RuntimeError):
    pass


def normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def clause_hash(clause):
    return hashlib.sha256(normalize(clause).encode("utf-8")).hexdigest()


def parse_chunk(js):
    """(date, clause) from the terms chunk's source."""
    m = DATE_RE.search(js)
    if not m:
        raise TermsError("no 'Last updated on <date>' in the terms chunk")
    k = js.find("Restricted Persons")
    if k < 0:
        raise TermsError("no 'Restricted Persons' in the terms chunk")
    start = js.rfind("`", 0, k)
    end = js.find("`", k)
    if start < 0 or end < 0:
        raise TermsError("the Restricted Persons clause is not a template string any more")
    clause = normalize(js[start + 1 : end])
    if len(clause) < 80 or "Restricted" not in clause:
        raise TermsError("the extracted clause is implausibly short")
    return m.group(1), clause


def fetch_terms(get=None, base=TERMS_URL, timeout=30):
    """Fetch and parse the live terms. Returns {date, clause, sha256, chunk_url}."""
    get = get or (lambda url: requests.get(url, headers=UA, timeout=timeout))

    def text(url):
        r = get(url)
        if getattr(r, "status_code", 200) != 200:
            raise TermsError(f"{url} answered {r.status_code}")
        r.encoding = "utf-8"  # the bundles are UTF-8; never let a guessed charset change the clause's hash
        return r.text

    html = text(base)
    scripts = re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html)
    bundles = [urljoin(base, s) for s in scripts if "/assets/" in s]
    if not bundles:
        raise TermsError("the app page names no /assets/ bundle")
    candidates, seen = [], set()
    for b in bundles:
        js = text(b)
        if "Restricted Persons" in js:
            candidates.insert(0, (b, js))
            break
        names = re.findall(r"assets/[A-Za-z0-9_.-]+\.js", js)
        ordered = sorted(set(names), key=lambda n: (0 if "erms" in n else 1, n))
        for n in ordered:
            url = urljoin(base, "/" + n)
            if url not in seen:
                seen.add(url)
                candidates.append((url, None))
    for url, js in candidates[:150]:
        js = js if js is not None else text(url)
        if "Restricted Persons" in js:
            date, clause = parse_chunk(js)
            return {"date": date, "clause": clause, "sha256": clause_hash(clause), "chunk_url": url}
    raise TermsError("no app chunk contains 'Restricted Persons'")


def check_terms(store, notifier, fetch=fetch_terms, clock=time.time):
    """The weekly check. Never flattens; on a change it blocks new exposure and alerts once."""
    st = store.get("terms") or {"baseline": dict(BASELINE), "failures": 0}
    now = clock()
    try:
        t = fetch()
    except Exception as exc:  # noqa: BLE001 - every failure counts toward the three-week alert
        st["failures"] = st.get("failures", 0) + 1
        st["last_error"] = f"{type(exc).__name__}: {exc}"
        st["last_checked"] = now
        if st["failures"] >= FAILURES_BEFORE_ALERT and not st.get("failure_alerted"):
            notifier.alert("terms-check-failing", "cannot read Hyperliquid's terms any more",
                           f"The weekly terms check failed {st['failures']} weeks in a row. Last error: "
                           f"{st['last_error']}. Trading continues under the terms last read on "
                           f"{st['baseline'].get('date')}.",
                           f"Ask your assistant to repair {DISPLAY_NAME}'s terms check.", window_hours=24 * 365)
            st["failure_alerted"] = True
        store.put("terms", st)
        return {"status": "failed", "failures": st["failures"], "error": st["last_error"]}
    st.update({"failures": 0, "failure_alerted": False, "last_checked": now, "last_seen": {
        k: t[k] for k in ("date", "sha256", "chunk_url")}})
    base = st["baseline"]
    if t["date"] == base.get("date") and t["sha256"] == base.get("sha256"):
        store.put("terms", st)
        return {"status": "unchanged", "date": t["date"], "sha256": t["sha256"]}
    if st.get("changed", {}).get("sha256") != t["sha256"] or st.get("changed", {}).get("date") != t["date"]:
        st["changed"] = {"date": t["date"], "sha256": t["sha256"], "clause": t["clause"], "seen_at": now}
        block = store.get("entry_block") or {}
        block["terms"] = {"since": now, "date": t["date"], "sha256": t["sha256"]}
        store.put("entry_block", block)
        notifier.alert(
            f"terms-changed:{t['date']}:{t['sha256'][:12]}",
            "Hyperliquid changed its terms; the bot stopped opening new positions",
            f"Terms now say 'Last updated on {t['date']}' (was {base.get('date')}). The Restricted Persons clause "
            f"now reads: \"{t['clause']}\" Existing positions are kept and may be reduced; nothing new is opened.",
            "Read it, check that you are still allowed to use Hyperliquid, then tell your assistant whether the bot "
            "may keep trading under the new terms.",
            window_hours=24 * 365,
        )
    store.put("terms", st)
    return {"status": "changed", "date": t["date"], "sha256": t["sha256"], "clause": t["clause"]}


def accept_terms(store):
    """Adopt the changed terms as the new baseline and lift the block (after someone has judged them)."""
    st = store.get("terms") or {"baseline": dict(BASELINE), "failures": 0}
    changed = st.pop("changed", None)
    if not changed:
        return None
    st["baseline"] = {"date": changed["date"], "sha256": changed["sha256"], "accepted_at": time.time()}
    store.put("terms", st)
    block = store.get("entry_block") or {}
    block.pop("terms", None)
    store.put("entry_block", block)
    return st["baseline"]
