"""Reading and writing the configuration: the four files, the instances in
two of them, the rule set, and validation.

Every write is validated as a whole and refused as a whole, written
atomically, and applied through the API whenever a daemon answers, as
``docs/architecture.md`` promises for every write path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from bricklogger.config import (
    CONFIG_FILES,
    RESERVED_INSTANCE_NAMES,
    FilesExist,
    InstanceNotFound,
    instances_in,
    read_texts,
    remove_instance,
    set_instance,
    validate_configuration,
    write_examples,
    write_text,
)
from bricklogger.ops.catalogue import describe
from bricklogger.ops.client import DaemonApi
from bricklogger.ops.context import Operations
from bricklogger.ops.errors import OperationError

YAML = "application/yaml"
ROLES = ("source", "destination")
REFERENCE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def file_of(role: str) -> str:
    """The file a role's instances live in."""
    if role not in ROLES:
        raise OperationError(f"the role is source or destination, not {role!r}")
    return "sources" if role == "source" else "destinations"


def file_name(name: str) -> str:
    if name not in CONFIG_FILES:
        raise OperationError(
            f"unknown configuration file {name!r}; one of {', '.join(CONFIG_FILES)}"
        )
    return name


def read_file(ops: Operations, client: DaemonApi | None, name: str) -> str:
    """One file as written, empty when it does not exist."""
    name = file_name(name)
    if client is not None:
        return client.get_text(f"/v1/config/{name}", accept=YAML)
    return read_texts(ops.config_dir)[name] or ""


def read_files(ops: Operations, client: DaemonApi | None) -> dict[str, str]:
    """All four files as written, empty where one does not exist."""
    return {name: read_file(ops, client, name) for name in CONFIG_FILES}


def validate(
    ops: Operations,
    client: DaemonApi | None,
    texts: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """The validation result for the files as they stand, or with ``texts``
    replacing some of them; through the API only when one is named."""
    if ops.api_url is not None and client is not None:
        result: dict[str, Any] = client.post(
            "/v1/config/validate", json=dict(texts) if texts else None
        )
        return result
    return validate_configuration(
        ops.config_dir, ops.plugins(), ops.env, texts
    ).as_dict()


def write_file(
    ops: Operations, client: DaemonApi | None, name: str, text: str
) -> dict[str, Any]:
    """Write one file validated as a whole, and return what the validation said.

    Through the API the daemon validates and applies; without one the directory
    is validated with the change in place of the file, and nothing is written
    when it does not hold.
    """
    name = file_name(name)
    if client is not None:
        answer: dict[str, Any] = client.put(f"/v1/config/{name}", text, YAML)
        return answer
    result = validate_configuration(
        ops.config_dir, ops.plugins(), ops.env, texts={name: text}
    )
    if not result.valid:
        raise OperationError(
            f"{name}.yaml not written: {_count(len(result.errors), 'error')}",
            result.errors,
        )
    write_text(ops.config_dir, name, text)
    return result.as_dict()


def write_example_files(ops: Operations, client: DaemonApi | None) -> list[str]:
    """The four files with commented examples; refused when any exists."""
    if ops.api_url is not None and client is not None:
        result = client.post("/v1/config/init")
        return [str(path) for path in result["written"]]
    try:
        written = write_examples(ops.config_dir)
    except FilesExist as exc:
        raise OperationError(str(exc)) from exc
    except OSError as exc:
        raise OperationError(f"could not write to {ops.config_dir}: {exc}") from exc
    return [str(path) for path in written]


def instances(ops: Operations, client: DaemonApi | None, role: str) -> dict[str, Any]:
    """The instances of one role, as written."""
    return instances_in(read_file(ops, client, file_of(role)))


def is_secret(spec: Mapping[str, Any]) -> bool:
    """Whether the plugin marks the setting as a secret: JSON Schema format password."""
    return spec.get("format") == "password"


def check_settings(
    declaration: Mapping[str, Any], settings: Mapping[str, Any]
) -> dict[str, Any]:
    """The settings a plugin's schema knows, with a secret only as ``${VAR}``.

    A key the schema does not have is refused with the ones it does, and a
    literal secret is refused, so it cannot end up in the file by accident.
    """
    properties: Mapping[str, Any] = (declaration.get("config_schema") or {}).get(
        "properties"
    ) or {}
    for key, value in settings.items():
        if key not in properties:
            known = ", ".join(properties) or "no settings"
            raise OperationError(
                f"{declaration['type']} has no setting {key!r}; it takes {known}"
            )
        if (
            value is not None
            and is_secret(properties[key])
            and not (isinstance(value, str) and REFERENCE.match(value))
        ):
            raise OperationError(
                f"{key!r} is a secret and is never written into the file; put its "
                "value in the env file of the config directory and give the setting "
                "as ${VARIABLE}"
            )
    return dict(settings)


def write_instance(
    ops: Operations,
    client: DaemonApi | None,
    role: str,
    action: str,
    name: str,
    type_name: str | None,
    settings: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add or edit an instance in its file: the settings given are merged into
    what stands, a ``None`` removes a key, and the instance is spliced into the
    text so comments and the other instances stay. Returns the instance as
    written and the validation result."""
    file = file_of(role)
    if name in RESERVED_INSTANCE_NAMES:
        raise OperationError(
            f"{name!r} is a subcommand of `bricklogger {file}` and cannot name an "
            "instance"
        )
    text = read_file(ops, client, file)
    current = instances_in(text).get(name)
    if action == "add" and current is not None:
        raise OperationError(f"{name!r} is already configured; change it with edit")
    if action == "edit" and current is None:
        raise OperationError(f"no {role} named {name!r}; write it with add")
    data: dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
    chosen = type_name or str(data.get("type") or "")
    if not chosen:
        raise OperationError(
            "a new instance needs a type; the catalogue lists the installed types"
        )
    declaration = describe(ops, client, chosen)
    if declaration["role"] != role:
        raise OperationError(f"{chosen!r} is a {declaration['role']}, not a {role}")
    data["type"] = chosen
    for key, value in check_settings(declaration, settings).items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    result = write_file(ops, client, file, set_instance(text, name, data))
    return data, result


def remove_instance_from_file(
    ops: Operations, client: DaemonApi | None, role: str, name: str
) -> dict[str, Any]:
    """Remove an instance from its file; the validation result comes back."""
    file = file_of(role)
    text = read_file(ops, client, file)
    try:
        without = remove_instance(text, name)
    except InstanceNotFound as exc:
        raise OperationError(f"no {role} named {name!r}") from exc
    return write_file(ops, client, file, without)


def write_rules(
    ops: Operations, client: DaemonApi | None, rules: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Replace the rule set with the rules given, written as a fresh file."""
    text = (
        yaml.safe_dump(
            [dict(rule) for rule in rules],
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        if rules
        else ""
    )
    return write_file(ops, client, "rules", text)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"
