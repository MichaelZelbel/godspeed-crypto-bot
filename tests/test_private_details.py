"""Nothing of a maintainer's own setup is in the tree (scripts/check-private-details.sh).

The check knows generic patterns (wallet addresses, addresses in private networks, a provider's default host names,
the old private tool names, the maintainer's name outside LICENSE). The maintainer's own host names and paths are
not in the public tree at all: they are in a file outside it that the check can be given
(CRYPTO_BOT_PRIVATE_PATTERNS), and the tests below use invented ones.
"""

import gzip
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-private-details.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

FIRST, LAST = "Mich" + "ael", "Zel" + "bel"  # the licence holder's name, which belongs in LICENSE only
REPO = FIRST + LAST + "/godspeed-crypto-bot"  # the public repository's own address: allowed
ADDRESS = "0x" + "04" + "3a" * 18 + "ff"  # any 40-digit hexadecimal address stands for a wallet
BURN = "0x" + "0" * 36 + "dE" + "aD"


def run(root, private=None, env_extra=None):
    import os

    env = dict(os.environ)
    env.pop("CRYPTO_BOT_PRIVATE_PATTERNS", None)
    if private is not None:
        env["CRYPTO_BOT_PRIVATE_PATTERNS"] = str(private)
    r = subprocess.run([BASH, str(SCRIPT), str(root)], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout


def test_the_tree_is_clean():
    code, out = run(ROOT)
    assert code == 0, out


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "LICENSE").write_text(f"Copyright (c) 2026 {FIRST} {LAST}\n")  # the one place the name belongs
    (tmp_path / "README.md").write_text(f"Install: curl https://raw.githubusercontent.com/{REPO}/v0.1.0/install.sh | bash\n")
    return tmp_path


def test_the_licence_holder_and_the_public_repository_address_are_allowed(tree):
    assert run(tree) == (0, "")


def test_the_websites_public_repository_address_is_allowed_but_no_other_repository(tree):
    (tree / "map.json").write_text(f"written in the website repository ({FIRST}{LAST}/godspeedmissioncontrol)\n")
    assert run(tree) == (0, "")
    (tree / "other.md").write_text(f"see {FIRST}{LAST}/some-private-notes\n")
    code, out = run(tree)
    assert code == 1 and "other.md" in out and "map.json" not in out


def test_burn_addresses_are_allowed(tree):
    (tree / "notes.md").write_text(f"{BURN} and 0x{'0' * 40}\n")
    assert run(tree) == (0, "")


def test_a_planted_wallet_address_fails(tree):
    (tree / "x.py").write_text(f'A = "{ADDRESS}"\n')
    code, out = run(tree)
    assert code == 1 and "x.py: a wallet address" in out


def test_the_licence_is_exempt_from_the_name_check_only(tree):
    (tree / "LICENSE").write_text(f"Copyright (c) 2026 {FIRST} {LAST}\nsee {ADDRESS}\n")
    code, out = run(tree)
    assert code == 1 and "LICENSE: a wallet address" in out and "LICENSE: a name" not in out


@pytest.mark.parametrize("line", [
    "server srv" + "1234567" + ".example", "the host hst" + "gr.cloud", "/mnt/" + "c/dev", "the old mc-" + "notify lane",
    "C:" + chr(92) + "god" + "speed" + chr(92) + "dev", "tailnet 100." + "86.49.3", "lan 192." + "168.1.20", "box 10." + "0.0.7",
])
def test_a_private_host_path_or_address_fails(tree, line):
    (tree / "y.md").write_text(line + "\n")
    code, out = run(tree)
    assert code == 1 and "y.md: a private host, path or tool" in out


def test_the_maintainers_own_list_is_read_from_outside_the_tree(tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "private.txt"
    outside.write_text("big-" + "iron-7\nmy-" + "secret-tool\n")  # invented stand-ins for host names and tools
    (tree / "n.md").write_text("ssh big-" + "iron-7\n")
    assert run(tree)[0] == 0  # without the list nothing knows the invented name
    code, out = run(tree, private=outside)
    assert code == 1 and "n.md: a private host, path or tool" in out


@pytest.mark.parametrize("line", [f"written by {FIRST}", f"contact {LAST.lower()}", f"{FIRST} {LAST}'s server"])
def test_the_name_outside_the_licence_fails(tree, line):
    (tree / "z.md").write_text(line + "\n")
    code, out = run(tree)
    assert code == 1 and "z.md: a name" in out


def test_a_private_detail_inside_a_compressed_file_fails(tree):
    with gzip.open(tree / "data.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(f'{{"owner": "{FIRST}"}}')
    code, out = run(tree)
    assert code == 1 and "data.json.gz: a name" in out
