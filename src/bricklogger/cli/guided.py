"""The questions ``init``, ``add`` and ``edit`` share.

A type's settings are asked from its configuration schema — required first,
optional with the default offered, or with the instance's current value when
one exists, so Enter keeps it. A setting the plugin marks as a secret is asked
for without echo and goes to the ``env`` file under a variable named after the
instance and the key; the YAML only ever gets ``${VARIABLE}``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

from bricklogger.cli.plugins import coerce_setting, is_secret
from bricklogger.config import ENV_FILE

_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def ask_settings(
    name: str,
    schema: Mapping[str, Any],
    secrets: dict[str, str],
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every setting of the schema, asked one by one.

    ``current`` gives the instance's settings as written when one exists; each
    is offered as the value Enter keeps. Secrets that are typed land in
    ``secrets`` by variable name, for the env file.
    """
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    present = dict(current or {})
    ordered = [k for k in properties if k in required] + [
        k for k in properties if k not in required
    ]
    settings: dict[str, Any] = {}
    for key in ordered:
        spec = properties[key]
        if spec.get("description"):
            typer.echo(f"  {spec['description']}")
        if is_secret(spec):
            value = _ask_secret(name, key, key in required, present.get(key), secrets)
            if value is not None:
                settings[key] = value
            continue
        if key in present:
            shown = json.dumps(present[key])
            answer = _prompt(f"  {key} [{shown}, Enter keeps it]")
            settings[key] = (
                present[key] if answer == "" else coerce_setting(key, answer, spec)
            )
        elif key in required:
            settings[key] = coerce_setting(key, str(typer.prompt(f"  {key}")), spec)
        else:
            default = spec.get("default")
            shown = "none" if default is None else json.dumps(default)
            answer = _prompt(f"  {key} [{shown}]")
            if answer != "":
                settings[key] = coerce_setting(key, answer, spec)
    return settings


def _ask_secret(
    name: str,
    key: str,
    required: bool,
    present: Any,
    secrets: dict[str, str],
) -> str | None:
    """A secret: kept where it is on Enter, or typed into the env file."""
    reference = _REFERENCE.match(str(present)) if isinstance(present, str) else None
    variable = reference.group(1) if reference else f"{name}_{key}".upper()
    variable = variable.replace("-", "_")
    if present is not None:
        hint = (
            f"kept in the env file as {variable}; Enter keeps it"
            if reference
            else "written in the file as it is; Enter keeps it, a value moves it "
            f"to the env file as {variable}"
        )
        answer = _prompt(f"  {key} ({hint})", hidden=True)
        if answer == "":
            return str(present)
    elif required:
        answer = str(
            typer.prompt(
                f"  {key} (kept in the env file as {variable})", hide_input=True
            )
        )
    else:
        answer = _prompt(
            f"  {key} (kept in the env file as {variable}; Enter for none)", hidden=True
        )
        if answer == "":
            return None
    secrets[variable] = answer
    return f"${{{variable}}}"


def _prompt(label: str, *, hidden: bool = False) -> str:
    return str(
        typer.prompt(label, default="", show_default=False, hide_input=hidden)
    ).strip()


def write_env(config_dir: Path, secrets: Mapping[str, str]) -> Path:
    """Write the secrets into the env file, replacing a variable that is there
    and appending the others; the file is created with mode 600."""
    path = config_dir / ENV_FILE
    exists = path.exists()
    lines = path.read_text(encoding="utf-8").splitlines() if exists else []
    remaining = dict(secrets)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        name = stripped.partition("=")[0].strip()
        if name in remaining:
            lines[index] = f"{name}={_quoted(remaining.pop(name))}"
    lines.extend(f"{name}={_quoted(value)}" for name, value in remaining.items())
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    if not exists:
        os.chmod(path, 0o600)
    return path


def _quoted(value: str) -> str:
    """Quoted only when the env file's reader would otherwise trim or misread it."""
    if value == value.strip() and value[:1] not in ("'", '"'):
        return value
    quote = "'" if '"' in value else '"'
    return f"{quote}{value}{quote}"
