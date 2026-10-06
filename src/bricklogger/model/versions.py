"""The model versions in the data directory.

Every upload is stored as it was uploaded, numbered in upload order, with a
small record beside it. The active version is recorded in a marker file next
to the version files, so the runtime state can be deleted without losing the
model. See ``docs/architecture.md``, "Lifecycle: upload, versions and
replacement".
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MODELS_DIR = "models"
ACTIVE_MARKER = "active"

_EXTENSIONS = {
    "turtle": "ttl",
    "xml": "rdf",
    "nt": "nt",
    "n3": "n3",
    "json-ld": "jsonld",
    "trig": "trig",
    "nquads": "nq",
}


class ModelNotFound(KeyError):
    """No stored version has this number."""

    def __init__(self, number: int) -> None:
        self.number = number
        super().__init__(f"model version {number} does not exist")

    def __str__(self) -> str:
        return f"model version {self.number} does not exist"


@dataclass(frozen=True)
class ModelVersion:
    """One stored upload."""

    number: int
    uploaded_at: datetime
    format: str
    size: int
    sha256: str
    path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.number,
            "uploaded_at": self.uploaded_at.isoformat(),
            "format": self.format,
            "size": self.size,
            "sha256": self.sha256,
        }


class ModelStore:
    """The versions under ``<data_dir>/models``."""

    def __init__(self, data_dir: Path) -> None:
        self.directory = data_dir / MODELS_DIR

    def versions(self) -> list[ModelVersion]:
        """Every stored version, oldest first."""
        if not self.directory.is_dir():
            return []
        found: list[ModelVersion] = []
        for record in sorted(self.directory.glob("[0-9]*.json")):
            found.append(self._read_record(record))
        return found

    def get(self, number: int) -> ModelVersion:
        record = self.directory / f"{number:04d}.json"
        if not record.is_file():
            raise ModelNotFound(number)
        return self._read_record(record)

    def read(self, number: int) -> bytes:
        """The model exactly as it was uploaded."""
        return self.get(number).path.read_bytes()

    def store(self, data: bytes, fmt: str) -> ModelVersion:
        """Store an upload as the next version; nothing is activated."""
        self.directory.mkdir(parents=True, exist_ok=True)
        versions = self.versions()
        number = versions[-1].number + 1 if versions else 1
        extension = _EXTENSIONS.get(fmt, "rdf")
        path = self.directory / f"{number:04d}.{extension}"
        version = ModelVersion(
            number=number,
            uploaded_at=datetime.now(UTC).replace(microsecond=0),
            format=fmt,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            path=path,
        )
        _write_atomically(path, data)
        record = {**version.as_dict(), "file": path.name}
        _write_atomically(
            self.directory / f"{number:04d}.json",
            (json.dumps(record, indent=2) + "\n").encode("utf-8"),
        )
        return version

    def active(self) -> int | None:
        """The number of the active version, or ``None`` when no model is active."""
        marker = self.directory / ACTIVE_MARKER
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8").strip()
        return int(text) if text else None

    def set_active(self, number: int) -> None:
        """Record the active version; the version must exist."""
        self.get(number)
        _write_atomically(self.directory / ACTIVE_MARKER, f"{number}\n".encode())

    def _read_record(self, record: Path) -> ModelVersion:
        data = json.loads(record.read_text(encoding="utf-8"))
        return ModelVersion(
            number=int(data["version"]),
            uploaded_at=datetime.fromisoformat(data["uploaded_at"]),
            format=str(data["format"]),
            size=int(data["size"]),
            sha256=str(data["sha256"]),
            path=self.directory / str(data["file"]),
        )


def _write_atomically(path: Path, data: bytes) -> None:
    """Write through a temporary file and rename, so a reader never sees half a file."""
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
