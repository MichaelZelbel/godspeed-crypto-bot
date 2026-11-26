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


CODE_NAME = re.compile(r"\bS[0-9]-(?:MAJ|HL(?:-[0-9]x)?)\b")


def test_the_program_and_its_notes_never_say_the_lab():
    """Developers read the comments, and a stranger reads the source too: it says "the research", never "the lab"."""
    found = []
    for path in [ROOT / "AGENTS.md", ROOT / "pyproject.toml", *sorted((ROOT / "godspeed_crypto_bot").rglob("*.py"))]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if LAB.search(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert not found, "\n".join(found)


def test_no_research_code_names_in_what_a_person_reads():
    """The books are told by what they do (trend, carry), never by the research's code names."""
    found = []
    for path in [ROOT / "AGENTS.md", *documents()]:
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if CODE_NAME.search(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    for path in sorted((ROOT / "godspeed_crypto_bot").rglob("*.py")):
        for n, text in runtime_strings(path):
            if CODE_NAME.search(text):
                found.append(f"{path.relative_to(ROOT)}:{n}: {text.strip()[:100]}")
    assert not found, "a research code name is shown to a person:\n" + "\n".join(found)


def test_the_installer_prints_sentences_and_keeps_data_in_its_log():
    """What the program prints as data (the preflight result, the first run) goes to the install log; the installer
    says only sentences, and on a failed check lists the problems as lines of words."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    for command in ('"/usr/local/sbin/$NAME" preflight', '"/usr/local/sbin/$NAME" run-daily'):
        calls = [ln for ln in text.splitlines() if command in ln and not ln.lstrip().startswith("#")]
        assert calls, command
        assert all("$log" in ln for ln in calls), f"output of {command} must go to the install log: {calls}"
    # a result held in a variable is written to the log, never printed with say/echo/printf to the terminal
    assert "printf '%s\\n' \"$said\" >> \"$log\"" in text


def test_the_installers_own_sentences_are_plain():
    """Every sentence the installer says or dies with has no braces, quoted keys or setting names."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    sentences = re.findall(r'\b(?:say|die) "((?:[^"\\]|\\.)*)"', text)
    assert len(sentences) > 20
    for s in sentences:
        plain = re.sub(r"\$\{[^}]*\}", "", s)  # a variable's value is not a data block
        assert not re.search(r'[{}]|\\"\w+\\":|\bagent_approved_on\b', plain), s
    assert re.search(r"[{}]", 'echo "{ \\"restricted\\": true }"')  # the check would see a data block


def test_the_expiry_problem_names_the_command_and_the_command_sets_it(tmp_path, capsys):
    import tomllib

    from godspeed_crypto_bot import cli

    p = tmp_path / "c.toml"
    p.write_text('mode = "paper"\n')
    assert cli.main(["--config", str(p), "configure", "--agent-approved-on", "2026-11-20"]) == 0
    capsys.readouterr()
    assert tomllib.loads(p.read_text())["agent_approved_on"] == "2026-11-20"
    for bad in ("tomorrow", "20261121", "2026-13-40"):
        code = cli.main(["--config", str(p), "configure", "--agent-approved-on", bad])
        assert code != 0, bad
        assert tomllib.loads(p.read_text())["agent_approved_on"] == "2026-11-20"  # a bad day changes nothing
