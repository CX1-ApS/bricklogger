"""The documentation, shipped inside the package: the pages by name, one page's
text, and an index with each page's title."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable

from bricklogger.ops.errors import OperationError


def _root() -> Traversable:
    return files("bricklogger") / "docs"


def pages() -> list[str]:
    """The pages by name, ``features/configuration`` for the configuration page."""
    found: list[str] = []

    def walk(node: Traversable, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda entry: entry.name):
            if child.is_dir():
                walk(child, f"{prefix}{child.name}/")
            elif child.name.endswith(".md"):
                found.append(f"{prefix}{child.name[:-3]}")

    walk(_root(), "")
    return found


def page(name: str) -> str:
    """One page as Markdown."""
    name = name.strip("/").removesuffix(".md")
    if name not in pages():
        raise OperationError(
            f"no documentation page {name!r}; the pages are: {', '.join(pages())}"
        )
    *folders, leaf = name.split("/")
    node = _root()
    for folder in folders:
        node = node / folder
    return (node / f"{leaf}.md").read_text(encoding="utf-8")


def title_of(name: str) -> str:
    """The page's first heading, or its name."""
    for line in page(name).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return name


def index() -> str:
    """The pages, one line each, with their titles."""
    lines = ["# Bricklogger documentation", ""]
    lines.extend(f"- `{name}`: {title_of(name)}" for name in pages())
    return "\n".join(lines) + "\n"
