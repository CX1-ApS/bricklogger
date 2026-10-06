"""What is wrong with a configuration: in which file, on which instance or rule,
at which key, and why."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError


class ConfigError(Exception):
    """A configuration operation could not be carried out."""


@dataclass(frozen=True)
class ConfigIssue:
    """One error or warning about the configuration.

    ``file`` is the file's name without extension (``daemon``, ``sources``,
    ``destinations`` or ``rules``); ``subject`` names the instance or rule the
    issue concerns, if any; ``key`` is the dotted key within it, if any.
    """

    file: str
    message: str
    subject: str | None = None
    key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "subject": self.subject,
            "key": self.key,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConfigIssue:
        """An issue as the API reports it."""
        return cls(
            file=str(data.get("file", "")),
            message=str(data.get("message", "")),
            subject=data.get("subject"),
            key=data.get("key"),
        )

    def __str__(self) -> str:
        where = f"{self.file}.yaml"
        if self.subject:
            where += f" [{self.subject}]"
        if self.key:
            where += f" {self.key}"
        return f"{where}: {self.message}"


_VALUE_ERROR_PREFIX = "Value error, "


def issues_from_validation_error(
    error: ValidationError, file: str, subject: str | None = None
) -> list[ConfigIssue]:
    """Turn pydantic's errors into issues that name the file, subject and key."""
    issues: list[ConfigIssue] = []
    for detail in error.errors():
        key = ".".join(str(part) for part in detail["loc"]) or None
        message = detail["msg"]
        if message.startswith(_VALUE_ERROR_PREFIX):
            message = message[len(_VALUE_ERROR_PREFIX) :]
        issues.append(ConfigIssue(file, message, subject, key))
    return issues
