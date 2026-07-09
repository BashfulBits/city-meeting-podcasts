from __future__ import annotations

import ast
from pathlib import Path

HOT_CONSUMERS = [
    "citypods/availability_digest.py",
    "citypods/bench.py",
    "citypods/compute/external_worker.py",
    "citypods/feeds.py",
    "citypods/ops/workqueue.py",
    "citypods/report.py",
    "citypods/run.py",
]

LEGACY_ATTRS = {"duration", "audio_duration_served"}
LEGACY_KEYS = {"duration", "duration_served"}
EPISODE_NAMES = {"ep", "episode"}


class LegacyDurationReadVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            node.attr in LEGACY_ATTRS
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id in EPISODE_NAMES
        ):
            self.hits.append((node.lineno, f"attribute:{node.attr}"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in LEGACY_KEYS
        ):
            self.hits.append((node.lineno, f"get:{node.args[0].value}"))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in LEGACY_KEYS
        ):
            self.hits.append((node.lineno, f"subscript:{node.slice.value}"))
        self.generic_visit(node)


def test_hot_consumers_use_duration_helpers_instead_of_legacy_fields():
    findings: list[str] = []
    for rel in HOT_CONSUMERS:
        tree = ast.parse(Path(rel).read_text(), filename=rel)
        visitor = LegacyDurationReadVisitor()
        visitor.visit(tree)
        findings.extend(f"{rel}:{line}:{kind}" for line, kind in visitor.hits)
    assert findings == []
