"""The entity document as an indented hierarchy: what ``model tree`` prints and
the MCP server's ``model_tree`` answers, built by the explorer's tree rule."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bricklogger.daemon.explorer import TreeNode

SORTS = ("name", "class", "count")


def render_tree(
    document: Mapping[str, Any], sort: str = "name", root: str | None = None
) -> str:
    """Per line the URI, the class, the name where it differs, what a grouping
    gathers, the findings in brackets, a point's outcome and instance, and its
    warnings after ``!``. A class heading in the grouping band is its class and
    the number beneath it; ``root`` names the element that was asked for, so
    that a grouping opens to its members instead of standing empty."""
    from bricklogger.daemon.explorer import build_tree, sort_tree
    from bricklogger.model.prefixes import UnknownPrefix, compact, expand

    if document.get("version") is None:
        return "no model is active"
    if not document["entities"]:
        return "no element matches"
    if root is not None:
        prefixes = document.get("prefixes") or {}
        with suppress(UnknownPrefix):
            root = compact(expand(root, prefixes), prefixes)
    roots = build_tree(document["entities"], document["relations"], root)
    sort_tree(roots, sort)
    lines: list[str] = []
    for node in roots:
        _add(lines, node, 0)
    counts = document["counts"]
    with_findings = sum(1 for entity in document["entities"] if entity["findings"])
    lines.append(
        f"{counts['entities']} entities, {counts['relations']} relations, "
        f"{with_findings} with findings"
    )
    return "\n".join(lines)


def _add(lines: list[str], node: TreeNode, level: int) -> None:
    lines.append("  " * level + _line(node))
    for child in node.children:
        _add(lines, child, level + 1)


def _line(node: TreeNode) -> str:
    entity = node.entity
    if entity is None:
        if node.label is None:
            return "Unplaced"
        return f"{node.label}  ({node.members})"
    parts = [str(node.uri)]
    if entity["class"]:
        parts.append(str(entity["class"]))
    local = str(node.uri).split(":", 1)[-1]
    if entity["name"] and entity["name"] != local:
        parts.append(f'"{entity["name"]}"')
    if entity.get("grouping") and node.members:
        parts.append(f"gathers {node.members}")
    if entity["findings"]:
        parts.append("[" + ",".join(entity["findings"]) + "]")
    runtime = entity["runtime"] or {}
    if runtime.get("outcome"):
        state = str(runtime["outcome"])
        if runtime.get("instance"):
            state += f" {runtime['instance']}"
        parts.append(state)
    elif runtime.get("accepted"):
        parts.append("accepted")
    # no_reference is both a finding on every point and the daemon's warning on
    # an accepted one: one name for one condition, so it is printed once.
    beside = [code for code in entity["warnings"] if code not in entity["findings"]]
    if beside:
        parts.append("!" + ",".join(beside))
    return "  ".join(parts)
