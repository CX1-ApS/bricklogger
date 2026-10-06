"""Validation of the whole config directory: the files themselves, every
instance against its plugin's schema, the rules' collection methods against
the installed sources, and the checks that span files."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from bricklogger.config.issues import ConfigIssue, issues_from_validation_error
from bricklogger.config.loader import load_configuration
from bricklogger.config.schema import (
    POLL,
    Configuration,
    DaemonSettings,
    Selector,
    is_loopback,
)
from bricklogger.model.versions import ModelStore
from bricklogger.sdk.registry import PluginRegistry


@dataclass
class ValidationResult:
    """The outcome of validating a config directory."""

    directory: Path
    errors: list[ConfigIssue] = field(default_factory=list)
    warnings: list[ConfigIssue] = field(default_factory=list)
    configuration: Configuration | None = None

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """The shape the API answers with: ``valid``, ``errors`` and ``warnings``."""
        return {
            "valid": self.valid,
            "errors": [issue.as_dict() for issue in self.errors],
            "warnings": [issue.as_dict() for issue in self.warnings],
        }


RESERVED_INSTANCE_NAMES = frozenset(
    {"status", "add", "edit", "remove", "start", "stop", "restart", "config"}
)
"""The subcommands of ``bricklogger sources`` and ``destinations``; an instance
named after one could not be reached, so validation refuses the name."""


def _unloadable(file: str, name: str, error: str) -> ConfigIssue:
    """A warning, not an error: the plugin is installed but did not load, so
    the instance will be failed while everything else runs."""
    return ConfigIssue(
        file,
        f"the plugin could not be loaded, so the instance will be failed: {error}",
        name,
        "type",
    )


def _reserved(file: str, name: str) -> ConfigIssue:
    return ConfigIssue(
        file,
        f"{name!r} is a subcommand of `bricklogger {file}` and cannot name an instance",
        name,
    )


def validate_configuration(
    config_dir: Path,
    registry: PluginRegistry,
    env: Mapping[str, str] | None = None,
    texts: Mapping[str, str | None] | None = None,
) -> ValidationResult:
    """Validate the whole directory; the configuration comes back only when valid.

    ``texts`` replaces files as they are on disk, for a proposed change.
    """
    loaded = load_configuration(config_dir, env, texts)
    if loaded.configuration is None:
        return ValidationResult(config_dir, errors=list(loaded.issues))
    config = loaded.configuration
    errors: list[ConfigIssue] = []
    warnings: list[ConfigIssue] = []

    daemon = config.daemon
    if not is_loopback(daemon.api.host) and not daemon.api.token:
        errors.append(
            ConfigIssue(
                "daemon",
                "required when api.host is not a loopback address",
                key="api.token",
            )
        )
    if not is_loopback(daemon.web.host) and not daemon.web.password:
        errors.append(
            ConfigIssue(
                "daemon",
                "required when web.host is not a loopback address",
                key="web.password",
            )
        )
    if not is_loopback(daemon.mcp.host) and not daemon.mcp.token:
        errors.append(
            ConfigIssue(
                "daemon",
                "required when mcp.host is not a loopback address",
                key="mcp.token",
            )
        )
    errors.extend(_validate_notifications(daemon))

    for name, source in config.sources.items():
        if name in RESERVED_INSTANCE_NAMES:
            errors.append(_reserved("sources", name))
        source_declaration = registry.sources.get(source.type)
        if source_declaration is None:
            failure = registry.failure_of(source.type)
            if failure is not None:
                warnings.append(_unloadable("sources", name, failure.error))
                continue
            errors.append(
                ConfigIssue(
                    "sources",
                    _unknown_type("source", source.type, registry.sources),
                    name,
                    "type",
                )
            )
            continue
        errors.extend(
            _validate_settings(
                source_declaration.config_schema, source.settings, "sources", name
            )
        )

    for name, destination in config.destinations.items():
        if name in RESERVED_INSTANCE_NAMES:
            errors.append(_reserved("destinations", name))
        destination_declaration = registry.destinations.get(destination.type)
        if destination_declaration is None:
            failure = registry.failure_of(destination.type)
            if failure is not None:
                warnings.append(_unloadable("destinations", name, failure.error))
                continue
            errors.append(
                ConfigIssue(
                    "destinations",
                    _unknown_type(
                        "destination", destination.type, registry.destinations
                    ),
                    name,
                    "type",
                )
            )
            continue
        errors.extend(
            _validate_settings(
                destination_declaration.config_schema,
                destination.settings,
                "destinations",
                name,
            )
        )

    offered = registry.source_methods()
    for index, rule in enumerate(config.rules):
        label = config.rule_label(index)
        candidates = [("method", rule.method)]
        if rule.fallback is not None:
            candidates.append(("fallback.method", rule.fallback.method))
        for key, method in candidates:
            if method is None or method == POLL or method in offered:
                continue
            available = ", ".join(sorted({POLL, *offered}))
            errors.append(
                ConfigIssue(
                    "rules",
                    f"unknown collection method {method!r}; the installed sources "
                    f"offer: {available}",
                    label,
                    key,
                )
            )
        if rule.match_regex is not None:
            errors.extend(_check_regexes(rule.match_regex, label))

    if (
        any(rule.action == "accept" for rule in config.rules)
        and not config.destinations
    ):
        warnings.append(
            ConfigIssue(
                "destinations",
                "the rule set accepts points, but no destination is configured",
            )
        )

    return ValidationResult(
        config_dir, errors, warnings, config if not errors else None
    )


def _check_regexes(selector: Selector, label: str) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for key, field_name in (
        ("class", "class_"),
        ("equipment", "equipment"),
        ("location", "location"),
        ("point", "point"),
    ):
        value = getattr(selector, field_name)
        expressions = [value] if isinstance(value, str) else (value or [])
        for expression in expressions:
            try:
                re.compile(expression)
            except re.error as exc:
                issues.append(
                    ConfigIssue(
                        "rules",
                        f"invalid regular expression {expression!r}: {exc}",
                        label,
                        f"match_regex.{key}",
                    )
                )
    return issues


def _validate_settings(
    schema: type[BaseModel], settings: dict[str, Any], file: str, subject: str
) -> list[ConfigIssue]:
    try:
        schema.model_validate(settings)
    except ValidationError as exc:
        return issues_from_validation_error(exc, file, subject)
    return []


def _unknown_type(role: str, type_name: str, known: Mapping[str, object]) -> str:
    installed = ", ".join(sorted(known)) or "none"
    return f"unknown {role} type {type_name!r}; installed: {installed}"


def _validate_notifications(daemon: DaemonSettings) -> list[ConfigIssue]:
    """Notifications need a server, a sender, a recipient and an active model.

    The model requirement is the point of the feature being opt-in: a daemon
    with nothing to collect has nothing to report, and the building name the
    subject carries is read from the model. See
    ``docs/features/notifications.md``, "Switching it on".
    """
    settings = daemon.notifications
    if not settings.enabled:
        return []
    issues: list[ConfigIssue] = []
    for key, value in (
        ("notifications.smtp.host", settings.smtp.host),
        ("notifications.from", settings.sender),
        ("notifications.to", settings.to),
    ):
        if not value:
            issues.append(
                ConfigIssue(
                    "daemon", "required when notifications are enabled", key=key
                )
            )
    if ModelStore(daemon.data_dir).active() is None:
        issues.append(
            ConfigIssue(
                "daemon",
                "notifications can only be enabled where a model is active; "
                "upload a model first",
                key="notifications.enabled",
            )
        )
    return issues
