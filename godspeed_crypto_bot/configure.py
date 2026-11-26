"""Editing the configuration file safely: one setting at a time, comments and the rest kept as they are.

Used by `go-live` (the mode) and `configure` (the settings an assistant sets for the owner). It rewrites the
matching top-level line, or adds the key before the first table. The result is parsed and validated before it
replaces the file, so a bad value changes nothing.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from . import config


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_value(path, key: str, value) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.splitlines()
    new = f"{key} = {_literal(value)}"
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    table = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
    for i in range(table):
        if pattern.match(lines[i]):
            comment = re.search(r"\s+#.*$", lines[i])
            lines[i] = new + (comment.group(0) if comment else "")
            break
    else:
        lines.insert(table, new)
    out = chr(10).join(lines) + chr(10)
    tomllib.loads(out)  # still valid TOML
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(out, encoding="utf-8")
    try:
        config.load(tmp)  # still a configuration the bot accepts
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
