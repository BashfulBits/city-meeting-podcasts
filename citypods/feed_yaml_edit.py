"""Line-level insertions into a feed YAML that leave the rest of the file byte-identical.

A ``yaml.safe_load`` / ``safe_dump`` round-trip would drop every comment in ``config/feeds/*.yml``
and reflow quoting and flow-style sequences, turning a one-line selector addition into a whole-file
rewrite. These feeds are hand-maintained and their comments carry the reasoning for individual
selectors, so an automated editor has to add its line and touch nothing else.

Only the two additive edits automated remediation needs are supported. Both are idempotent, and
:func:`assert_only_addition` lets a caller prove an edit changed the parsed document in exactly
the intended way before writing it to disk.
"""

from __future__ import annotations

import json
from typing import Any

import yaml


def _yaml_scalar(value: str) -> str:
    """A double-quoted YAML scalar. JSON string escaping is a valid subset."""
    return json.dumps(value, ensure_ascii=False)


def _block_bounds(lines: list[str], key: str) -> tuple[int, int, str]:
    """``(header index, end index, child indent)`` for a top-level ``key:`` block.

    ``end`` is one past the block's last non-blank line, so an insertion at ``end`` lands inside
    the block rather than after a trailing blank line.
    """
    header = next((i for i, line in enumerate(lines) if line.rstrip() == f"{key}:"), -1)
    if header == -1:
        raise ValueError(f"no top-level {key!r} block found")

    end = header + 1
    child_indent = ""
    for i in range(header + 1, len(lines) + 1):
        if i == len(lines):
            break
        line = lines[i]
        if not line.strip():
            continue
        indent = line[: len(line) - len(line.lstrip())]
        if len(indent) == 0:
            break
        if not child_indent:
            child_indent = indent
        end = i + 1
    if not child_indent:
        raise ValueError(f"{key!r} block is empty")
    return header, end, child_indent


def _subkey_items_end(lines: list[str], start: int, end: int, indent: str, key: str) -> int:
    """Index one past the last line belonging to ``indent + key:`` within a block, or -1."""
    inline = next(
        (
            i
            for i in range(start, end)
            if lines[i].startswith(f"{indent}{key}:") and lines[i].rstrip() != f"{indent}{key}:"
        ),
        -1,
    )
    if inline != -1:
        raise ValueError(f"{key!r} is written inline; line-level insertion is not supported")
    header = next(
        (i for i in range(start, end) if lines[i].rstrip() == f"{indent}{key}:"),
        -1,
    )
    if header == -1:
        return -1
    last = header + 1
    for i in range(header + 1, end):
        line = lines[i]
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= len(indent):
            break
        last = i + 1
    return last


def _split(text: str) -> tuple[list[str], bool]:
    trailing_newline = text.endswith("\n")
    return text.split("\n")[:-1] if trailing_newline else text.split("\n"), trailing_newline


def _join(lines: list[str], trailing_newline: bool) -> str:
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def add_body_any(text: str, value: str) -> str:
    """Append ``value`` to ``source.body_any``, creating the key if absent."""
    lines, trailing = _split(text)
    header, end, indent = _block_bounds(lines, "source")
    item_indent = indent * 2

    items_end = _subkey_items_end(lines, header + 1, end, indent, "body_any")
    if items_end == -1:
        lines[end:end] = [f"{indent}body_any:", f"{item_indent}- {_yaml_scalar(value)}"]
    else:
        lines[items_end:items_end] = [f"{item_indent}- {_yaml_scalar(value)}"]
    return _join(lines, trailing)


def add_body_include(text: str, provider_guid: str, body: str) -> str:
    """Append a ``{provider_guid, body}`` mapping to ``source.body_includes``."""
    lines, trailing = _split(text)
    header, end, indent = _block_bounds(lines, "source")
    item_indent = indent * 2
    entry = [
        f"{item_indent}- provider_guid: {_yaml_scalar(provider_guid)}",
        f"{item_indent}  body: {_yaml_scalar(body)}",
    ]

    items_end = _subkey_items_end(lines, header + 1, end, indent, "body_includes")
    if items_end == -1:
        lines[end:end] = [f"{indent}body_includes:", *entry]
    else:
        lines[items_end:items_end] = entry
    return _join(lines, trailing)


def assert_only_addition(before: str, after: str, path: tuple[str, ...], added: Any) -> None:
    """Prove the edit added exactly ``added`` at ``path`` and changed nothing else.

    Guards the line-level editors against a malformed file shape silently producing a valid but
    wrong document -- the caller can refuse to write when this raises.
    """
    old = yaml.safe_load(before) or {}
    new = yaml.safe_load(after) or {}

    cursor_old: Any = old
    cursor_new: Any = new
    for key in path[:-1]:
        cursor_old = (cursor_old or {}).get(key) or {}
        cursor_new = (cursor_new or {}).get(key) or {}
    leaf = path[-1]

    expected = list(cursor_old.get(leaf) or []) + [added]
    if cursor_new.get(leaf) != expected:
        raise ValueError(f"edit did not append cleanly to {'.'.join(path)}")

    # Everything outside the edited list must be untouched.
    stripped_old = yaml.safe_load(before) or {}
    stripped_new = yaml.safe_load(after) or {}
    holder_old: Any = stripped_old
    holder_new: Any = stripped_new
    for key in path[:-1]:
        holder_old = holder_old.get(key) or {}
        holder_new = holder_new.get(key) or {}
    holder_old.pop(leaf, None)
    holder_new.pop(leaf, None)
    if stripped_old != stripped_new:
        raise ValueError(f"edit changed keys outside {'.'.join(path)}")
