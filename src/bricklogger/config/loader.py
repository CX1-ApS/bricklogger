"""Reading the config directory: the four YAML files, missing ones read as
empty, environment variables filled in, and the result validated into a
:class:`~bricklogger.config.schema.Configuration`."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from bricklogger.config.issues import ConfigIssue, issues_from_validation_error
from bricklogger.config.schema import (
    Configuration,
    DaemonSettings,
    DestinationInstance,
    Rule,
    SourceInstance,
    rule_label,
)

CONFIG_FILES: tuple[str, ...] = ("daemon", "sources", "destinations", "rules")
"""The four files, by name without extension, in the order they are reported."""

SYSTEM_CONFIG_DIR = Path("/etc/bricklogger")
"""The config directory in the container, which names it with the variable."""

SYSTEM_DATA_DIR = Path("/var/lib/bricklogger")
"""The data directory that belongs to it."""

CONFIG_DIR_ENV = "BRICKLOGGER_CONFIG_DIR"

ENV_FILE = "env"
"""The file in the config directory that carries the secrets."""

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def user_config_dir(home: Path | None = None) -> Path:
    """Where an ordinary login keeps its configuration."""
    return (Path.home() if home is None else home) / ".config" / "bricklogger"


def user_data_dir(home: Path | None = None) -> Path:
    """Where an ordinary login keeps its data."""
    return (Path.home() if home is None else home) / ".local" / "share" / "bricklogger"


def resolve_config_dir(
    cli_value: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The flag, then the environment variable, then the one in the home
    directory."""
    if cli_value is not None:
        return cli_value
    environment = os.environ if env is None else env
    from_env = environment.get(CONFIG_DIR_ENV)
    if from_env:
        return Path(from_env)
    return user_config_dir(home)


def default_data_dir(config_dir: Path, home: Path | None = None) -> Path:
    """Where the data goes when ``daemon.yaml`` does not say: beside the
    container's configuration in ``/var/lib``, otherwise in the home
    directory."""
    return SYSTEM_DATA_DIR if config_dir == SYSTEM_CONFIG_DIR else user_data_dir(home)


def read_env_file(config_dir: Path) -> dict[str, str]:
    """The ``env`` file's ``NAME=value`` lines; empty when the file is not there.

    Blank lines and comments are skipped, a leading ``export`` is allowed and a
    value may be quoted. The file carries the secrets the configuration
    interpolates; a variable already in the process environment wins over it.
    """
    path = config_dir / ENV_FILE
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        name, separator, value = stripped.partition("=")
        name = name.strip()
        if not separator or not name:
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def config_file(config_dir: Path, name: str) -> Path:
    """The path of one of the four files."""
    if name not in CONFIG_FILES:
        raise ValueError(f"unknown configuration file {name!r}")
    return config_dir / f"{name}.yaml"


def read_texts(config_dir: Path) -> dict[str, str | None]:
    """The files as written, ``None`` where a file does not exist."""
    texts: dict[str, str | None] = {}
    for name in CONFIG_FILES:
        path = config_file(config_dir, name)
        texts[name] = path.read_text(encoding="utf-8") if path.is_file() else None
    return texts


def write_text(config_dir: Path, name: str, text: str) -> Path:
    """Write one file verbatim and atomically: a temporary file, then a rename.

    An existing file keeps its permissions; a new one gets the usual ones.
    """
    path = config_file(config_dir, name)
    config_dir.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=config_dir, prefix=f".{name}.", suffix=".yaml.tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        if path.exists():
            shutil.copymode(path, temporary)
        else:
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def empty_content(name: str) -> Any:
    """What a missing or empty file means: no rules, or no keys."""
    return [] if name == "rules" else {}


def parse_text(name: str, text: str | None) -> tuple[Any, list[ConfigIssue]]:
    """Parse one file's YAML into raw data, without filling in environment variables.

    A missing or empty file is read as empty. The result is checked to be a
    mapping, or a list for ``rules``.
    """
    if text is None or not text.strip():
        return empty_content(name), []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return empty_content(name), [
            ConfigIssue(name, f"not valid YAML: {_yaml_message(exc)}")
        ]
    if data is None:
        return empty_content(name), []
    if name == "rules":
        if not isinstance(data, list):
            return empty_content(name), [
                ConfigIssue(name, "the file must contain a list of rules")
            ]
    elif not isinstance(data, dict):
        return empty_content(name), [
            ConfigIssue(name, "the file must contain a mapping")
        ]
    return data, []


def _yaml_message(exc: yaml.YAMLError) -> str:
    problem = getattr(exc, "problem", None) or str(exc)
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"
    return str(problem)


def interpolate(
    data: Any,
    env: Mapping[str, str],
    file: str,
    subject: str | None = None,
    key: tuple[str, ...] = (),
) -> tuple[Any, list[ConfigIssue]]:
    """Fill in ``${NAME}`` in every string, reporting the variables that are not set."""
    if isinstance(data, str):
        issues: list[ConfigIssue] = []

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in env:
                return env[name]
            issues.append(
                ConfigIssue(
                    file,
                    f"environment variable {name} is not set",
                    subject,
                    ".".join(key) or None,
                )
            )
            return match.group(0)

        return _ENV_REFERENCE.sub(replace, data), issues
    if isinstance(data, dict):
        result: dict[Any, Any] = {}
        issues = []
        for child_key, child in data.items():
            value, child_issues = interpolate(
                child, env, file, subject, (*key, str(child_key))
            )
            result[child_key] = value
            issues.extend(child_issues)
        return result, issues
    if isinstance(data, list):
        items: list[Any] = []
        issues = []
        for index, child in enumerate(data):
            value, child_issues = interpolate(
                child, env, file, subject, (*key, str(index))
            )
            items.append(value)
            issues.extend(child_issues)
        return items, issues
    return data, []


@dataclass
class LoadResult:
    """A loaded configuration, or the issues that prevented it."""

    directory: Path
    configuration: Configuration | None
    issues: list[ConfigIssue] = field(default_factory=list)


def load_configuration(
    config_dir: Path,
    env: Mapping[str, str] | None = None,
    texts: Mapping[str, str | None] | None = None,
) -> LoadResult:
    """Read, parse, fill in and validate the directory's four files.

    Every issue found is reported; the configuration is returned only when
    there is none. ``texts`` replaces what is read from the directory, file
    by file, so a proposed change can be validated before it is written.

    Secrets come from the directory's ``env`` file and from the environment,
    where the environment wins; ``data_dir`` defaults to the one that belongs
    to this config directory.
    """
    environment: Mapping[str, str] = {
        **read_env_file(config_dir),
        **(os.environ if env is None else env),
    }
    files = read_texts(config_dir)
    if texts:
        files.update(texts)
    raw: dict[str, Any] = {}
    issues: list[ConfigIssue] = []
    for name in CONFIG_FILES:
        data, file_issues = parse_text(name, files[name])
        raw[name] = data
        issues.extend(file_issues)
    if issues:
        return LoadResult(config_dir, None, issues)

    daemon_data, daemon_issues = interpolate(raw["daemon"], environment, "daemon")
    issues.extend(daemon_issues)
    if isinstance(daemon_data, dict) and daemon_data.get("data_dir") is None:
        daemon_data = {**daemon_data, "data_dir": str(default_data_dir(config_dir))}
    daemon: DaemonSettings | None = None
    try:
        daemon = DaemonSettings.model_validate(daemon_data)
    except ValidationError as exc:
        issues.extend(issues_from_validation_error(exc, "daemon"))

    sources: dict[str, SourceInstance] = {}
    for name, item in raw["sources"].items():
        item_data, item_issues = interpolate(item, environment, "sources", str(name))
        issues.extend(item_issues)
        try:
            sources[str(name)] = SourceInstance.model_validate(item_data)
        except ValidationError as exc:
            issues.extend(issues_from_validation_error(exc, "sources", str(name)))

    destinations: dict[str, DestinationInstance] = {}
    for name, item in raw["destinations"].items():
        item_data, item_issues = interpolate(
            item, environment, "destinations", str(name)
        )
        issues.extend(item_issues)
        try:
            destinations[str(name)] = DestinationInstance.model_validate(item_data)
        except ValidationError as exc:
            issues.extend(issues_from_validation_error(exc, "destinations", str(name)))

    rules: list[Rule] = []
    for index, item in enumerate(raw["rules"]):
        label = rule_label(index, item.get("name") if isinstance(item, dict) else None)
        item_data, item_issues = interpolate(item, environment, "rules", label)
        issues.extend(item_issues)
        try:
            rules.append(Rule.model_validate(item_data))
        except ValidationError as exc:
            issues.extend(issues_from_validation_error(exc, "rules", label))

    if issues or daemon is None:
        return LoadResult(config_dir, None, issues)
    return LoadResult(
        config_dir,
        Configuration(
            daemon=daemon, sources=sources, destinations=destinations, rules=rules
        ),
    )
