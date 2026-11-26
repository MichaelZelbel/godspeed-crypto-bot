"""Words a person reads never name the private research the strategy came from ("the lab").

A person meets the README, the assistant's instructions, the recipe, the unit descriptions and the configuration
example, and every string the program prints, writes to the page or sends. Comments and docstrings describe the
vendored research for developers and are left out.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAB = re.compile(r"\blab\b|\blab's\b|\blabs\b", re.I)


def documents():
    yield from (ROOT / name for name in ("README.md", "INSTALL-FOR-ASSISTANTS.md", "install.sh"))
    yield from sorted(p for p in (ROOT / "skill").rglob("*") if p.is_file())
    yield from sorted(p for p in (ROOT / "deploy").rglob("*") if p.is_file() and p.suffix != ".txt")


def runtime_strings(path):
    """Every string constant in a module except docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def test_no_lab_in_what_a_person_reads():
    found = []
    for path in documents():
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if LAB.search(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    for path in sorted((ROOT / "godspeed_crypto_bot").rglob("*.py")):
        for n, text in runtime_strings(path):
            if LAB.search(text):
                found.append(f"{path.relative_to(ROOT)}:{n}: {text.strip()[:100]}")
    assert not found, "the private research is named:\n" + "\n".join(found)


def test_the_check_would_see_a_lab_in_a_printed_string(tmp_path):
    module = tmp_path / "m.py"
    module.write_text('"""A docstring about the lab is for developers."""\n\nWORDS = "the lab\'s assumptions"\n',
                      encoding="utf-8")
    strings = [text for _n, text in runtime_strings(module)]
    assert strings == ["the lab's assumptions"]


def test_no_lab_in_the_commands_help(capsys):
    """The CLI's description is its module docstring, which the scan above leaves out: read the help itself."""
    import pytest

    from godspeed_crypto_bot import cli

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    assert cli.__doc__.splitlines()[0] in text and not LAB.search(text)
