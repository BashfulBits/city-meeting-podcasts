"""Narrow additive YAML edits with exact semantic assertions and original comments retained."""

from __future__ import annotations

import copy

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

from citypods.provider_catalog.apply import SOURCE_PATHS, EditPlan
from citypods.provider_catalog.evidence import digest


def load_config(text):
    if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(text)):
        raise ValueError("YAML aliases/anchors are unsupported for automated edits")
    node = yaml.compose(text)

    def validate(n):
        if isinstance(n, MappingNode):
            if any(not isinstance(k, ScalarNode) or not k.tag.endswith(":str") for k, _ in n.value):
                raise ValueError("only string mapping keys are supported")
            keys = [k.value for k, _ in n.value]
            if len(keys) != len(set(keys)) or "<<" in keys:
                raise ValueError("duplicate/merge YAML keys are unsupported")
            for _, child in n.value:
                validate(child)
        elif isinstance(n, SequenceNode):
            for child in n.value:
                validate(child)

    if node:
        validate(node)
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("config must be a mapping")
    return value


def _node(text, path):
    node = yaml.compose(text)
    for key in path:
        if not isinstance(node, MappingNode) or node.flow_style:
            raise ValueError("expected a block mapping")
        node = next((v for k, v in node.value if k.value == key), None)
        if node is None:
            return None
    return node


def _append(text, path, values):
    if not values:
        return text
    node = _node(text, path)
    parent = _node(text, path[:-1]) if len(path) > 1 else yaml.compose(text)
    if not isinstance(parent, MappingNode) or parent.flow_style:
        raise ValueError("expected a block parent mapping")
    indent = parent.start_mark.column
    key = path[-1]
    if node is None:
        # Insert immediately after the preceding value, retaining following comments verbatim.
        mark = parent.value[-1][1].end_mark
        index = (
            mark.index - mark.column if mark.column <= indent else text.find("\n", mark.index) + 1
        )
        if index == 0:
            index = len(text)
        payload = (
            " " * indent + key + ":\n" + yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
        )
        lines = payload.splitlines()
        payload = lines[0] + "\n" + "\n".join(" " * (indent + 2) + x for x in lines[1:]) + "\n"
        return (
            text[:index]
            + ("\n" if index and text[index - 1] != "\n" else "")
            + payload
            + text[index:]
        )
    if isinstance(node, ScalarNode) and node.tag.endswith(":null"):
        raise ValueError("null lists are unsupported; declare [] explicitly")
    if not isinstance(node, SequenceNode):
        raise ValueError("expected a YAML sequence")
    if node.flow_style:
        if node.value:
            # Flow lists remain flow lists; only insert new quoted values before the closing ].
            payload = yaml.safe_dump(values, default_flow_style=True, width=100000).strip()[1:-1]
            index = node.end_mark.index - 1
            return text[:index] + ", " + payload + text[index:]
        index, end = node.start_mark.index, node.end_mark.index
        # An empty inline list can become a block list without losing its inline comment.
        comment_end = text.find("\n", end)
        comment_end = len(text) if comment_end < 0 else comment_end
        suffix = text[end:comment_end]
        payload = "\n" + "\n".join(
            " " * (indent + 2) + x
            for x in yaml.safe_dump(values, sort_keys=False).strip().splitlines()
        )
        return text[:index].rstrip(" ") + suffix + payload + text[comment_end:]
    index = node.end_mark.index - node.end_mark.column
    payload = "\n".join(
        " " * node.start_mark.column + x
        for x in yaml.safe_dump(values, sort_keys=False, allow_unicode=True).strip().splitlines()
    )
    return text[:index] + payload + "\n" + text[index:]


def apply_config_edits(texts, plan: EditPlan):
    if any(digest(texts[p]) != stamp for p, stamp in plan.config_hashes):
        raise ValueError("config changed since planning")
    expected = {p: copy.deepcopy(load_config(texts[p])) for p in SOURCE_PATHS}
    output = dict(texts)
    limits, site, decisions = (expected[p] for p in SOURCE_PATHS)
    additions = []
    for pairs in plan.routes:
        route = dict(pairs)
        if any(r["route_id"] == route["route_id"] for r in limits["routes"]):
            raise ValueError("route ID collision")
        limits["routes"].append(route)
        additions.append(route)
    output[SOURCE_PATHS[0]] = _append(output[SOURCE_PATHS[0]], ("routes",), additions)
    for lane, model in plan.backups:
        block = site["llm_lanes"][lane]
        if model in (block.get("backup_models") or []):
            continue
        block.setdefault("backup_models", []).append(model)
        output[SOURCE_PATHS[1]] = _append(
            output[SOURCE_PATHS[1]], ("llm_lanes", lane, "backup_models"), [model]
        )
    additions = []
    if plan.ignored and "ignored" in decisions and not isinstance(decisions["ignored"], list):
        raise ValueError("ignored must be a list")
    for provider, model, stamp in plan.ignored:
        if any(
            x.get("provider") == provider and x.get("model") == model
            for x in decisions.get("ignored") or []
        ):
            continue
        entry = {
            "provider": provider,
            "model": model,
            "reason": "Maintainer /apply selection",
            "decided_on": stamp,
        }
        decisions.setdefault("ignored", []).append(entry)
        additions.append(entry)
    output[SOURCE_PATHS[2]] = _append(output[SOURCE_PATHS[2]], ("ignored",), additions)
    for path in SOURCE_PATHS:
        if load_config(output[path]) != expected[path]:
            raise ValueError(f"unexpected semantic change in {path}")
    return output
