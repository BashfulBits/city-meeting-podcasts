"""Narrow additive YAML edits with exact semantic assertions and original comments retained."""

from __future__ import annotations

import copy
import re

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, ScalarToken

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


def _set_route_free(text, route_id):
    routes = _node(text, ("routes",))
    if not isinstance(routes, SequenceNode) or routes.flow_style:
        raise ValueError("paid edits require a block route sequence")
    matches = [
        n
        for n in routes.value
        if isinstance(n, MappingNode)
        and any(k.value == "route_id" and v.value == route_id for k, v in n.value)
    ]
    if len(matches) != 1 or matches[0].flow_style:
        raise ValueError("paid route missing or unsupported")
    node = next((v for k, v in matches[0].value if k.value == "free"), None)
    if not isinstance(node, ScalarNode) or not node.tag.endswith(":bool"):
        raise ValueError("paid route requires an explicit boolean free field")
    return text[: node.start_mark.index] + "false" + text[node.end_mark.index :]


def _remove_routes(text, route_ids):
    """Remove exact block items, retaining comments and text outside those items."""
    if not route_ids:
        return text
    routes = _node(text, ("routes",))
    if not isinstance(routes, SequenceNode) or routes.flow_style:
        raise ValueError("removal requires a block route sequence")
    spans = []

    def last_value(node):
        if isinstance(node, MappingNode):
            return last_value(node.value[-1][1])
        if isinstance(node, SequenceNode) and node.value:
            return last_value(node.value[-1])
        return node

    for rid in route_ids:
        matches = [
            node
            for node in routes.value
            if isinstance(node, MappingNode)
            and any(k.value == "route_id" and v.value == rid for k, v in node.value)
        ]
        if len(matches) != 1 or matches[0].flow_style:
            raise ValueError("removal route missing or unsupported")
        node = matches[0]
        start = node.start_mark.index - node.start_mark.column
        end_mark = last_value(node).end_mark
        end = end_mark.index if end_mark.column == 0 else text.find("\n", end_mark.index)
        if end < 0:
            end = len(text)
        elif end_mark.column != 0:
            end += 1
        spans.append((start, end))
    if len(set(route_ids)) != len(route_ids):
        raise ValueError("duplicate route removals")
    if len(spans) == len(routes.value):
        raise ValueError("removal cannot empty the route catalog")
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


def _set_lane_field(text, lane, field, value):
    """Change one existing field, keeping surrounding text and YAML comments."""
    parent = _node(text, ("llm_lanes", lane))
    if not isinstance(parent, MappingNode) or parent.flow_style:
        raise ValueError("lane repair requires a block mapping")
    pair = next(((k, v) for k, v in parent.value if k.value == field), None)
    if pair is None:
        if value is None:
            return text
        raise ValueError("lane repair field missing")
    key, node = pair
    if value is None:
        start = key.start_mark.index - key.start_mark.column
    else:
        start = node.start_mark.index

    def last(node):
        if isinstance(node, MappingNode) and node.value:
            return last(node.value[-1][1])
        if isinstance(node, SequenceNode) and node.value:
            return last(node.value[-1])
        return node

    mark = last(node).end_mark
    end = mark.index
    if value is None:
        if mark.column:
            newline = text.find("\n", end)
            end = len(text) if newline < 0 else newline + 1
    # Scalars can contain '#'; only comments outside scalar spans are retained as comments.
    scalars = [
        (t.start_mark.index, t.end_mark.index)
        for t in yaml.scan(text)
        if isinstance(t, ScalarToken)
    ]
    comments = []
    position = start
    for line in text[start:end].splitlines(keepends=True):
        for column, char in enumerate(line):
            index = position + column
            if char == "#" and not any(a <= index < b for a, b in scalars):
                comments.append(line[column:].rstrip("\r\n"))
                break
        position += len(line)
    indent = key.start_mark.column
    if value is None:
        replacement = "".join(" " * indent + comment + "\n" for comment in comments)
    else:
        replacement = yaml.safe_dump(value, default_flow_style=True, width=100000).strip()
        if isinstance(node, SequenceNode) and not node.flow_style:
            # YAML permits indentless block lists; flow lists require a deeper value indent.
            replacement = " " * max(0, indent + 2 - node.start_mark.column) + replacement
        replacement += "".join("\n" + " " * indent + comment for comment in comments)
    return text[:start] + replacement + text[end:]


def apply_config_edits(texts, plan: EditPlan):
    if any(digest(texts[p]) != stamp for p, stamp in plan.config_hashes):
        raise ValueError("config changed since planning")
    expected = {p: copy.deepcopy(load_config(texts[p])) for p in SOURCE_PATHS}
    output = dict(texts)
    limits, site, decisions = (expected[p] for p in SOURCE_PATHS)
    if plan.rate_changes and (
        plan.proposal_kind != "limits"
        or any(
            (
                plan.routes,
                plan.backups,
                plan.ignored,
                plan.paid_routes,
                plan.acknowledged,
                plan.removed_routes,
                plan.lane_repairs,
            )
        )
    ):
        raise ValueError("rate proposals cannot mix policy or catalog edits")
    if plan.context_changes:
        if plan.proposal_kind != "context" or any(
            (
                plan.rate_changes,
                plan.routes,
                plan.backups,
                plan.ignored,
                plan.paid_routes,
                plan.acknowledged,
                plan.removed_routes,
                plan.lane_repairs,
            )
        ):
            raise ValueError("context proposals cannot mix other edits")
        seen = set()
        for rid, field, old, new, evidence_digest in plan.context_changes:
            if (
                (rid, field) in seen
                or field not in {"hard_input_ceiling", "output_context_limit"}
                or type(new) is not int
                or not 0 < new <= 2**53 - 1
                or not isinstance(evidence_digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", evidence_digest)
            ):
                raise ValueError("invalid context scalar change")
            seen.add((rid, field))
            matching = [r for r in limits["routes"] if r.get("route_id") == rid]
            if (
                len(matching) != 1
                or matching[0].get("free") is not True
                or matching[0].get("rpd") == 0
                or matching[0].get(field) != old
                or (old is not None and (type(old) is not int or old <= 0))
            ):
                raise ValueError("context route or old scalar changed")
            nodes = _node(output[SOURCE_PATHS[0]], ("routes",))
            if not isinstance(nodes, SequenceNode) or nodes.flow_style:
                raise ValueError("context route requires a block sequence")
            parent = next(
                n
                for n in nodes.value
                if isinstance(n, MappingNode)
                and any(k.value == "route_id" and v.value == rid for k, v in n.value)
            )
            if parent.flow_style:
                raise ValueError("context route requires a block mapping")
            node = next((v for k, v in parent.value if k.value == field), None)
            text = output[SOURCE_PATHS[0]]
            if node is None:
                if old is not None or field != "hard_input_ceiling":
                    raise ValueError("context optional scalar insertion is not permitted")
                anchor = next(k for k, _v in parent.value if k.value == "route_id")
                end = text.find("\n", anchor.start_mark.index)
                if end < 0:
                    raise ValueError("context insertion needs a block line")
                text = (
                    text[: end + 1]
                    + " " * anchor.start_mark.column
                    + f"{field}: {new}\n"
                    + text[end + 1 :]
                )
            else:
                if not isinstance(node, ScalarNode) or node.tag != "tag:yaml.org,2002:int":
                    raise ValueError("context edits require integer scalars")
                text = text[: node.start_mark.index] + str(new) + text[node.end_mark.index :]
            output[SOURCE_PATHS[0]] = text
            matching[0][field] = new
    targets = set()
    for change in plan.rate_changes:
        from citypods.provider_catalog.evidence import provider_rate_digest

        identity = (change.scope, change.target, change.metric)
        if identity in targets or change.metric not in {"rpm", "tpm", "rpd"}:
            raise ValueError("duplicate or unsupported rate scalar")
        targets.add(identity)
        if change.scope == "route":
            matches = [r for r in limits["routes"] if r.get("route_id") == change.target]
            if len(matches) != 1 or matches[0].get("provider") != change.provider:
                raise ValueError("rate route missing or mismatched")
            block = matches[0]
            # Check against the original source, before any same-route scalar edits.
            original = load_config(texts[SOURCE_PATHS[0]])
            stamp = digest(next(r for r in original["routes"] if r["route_id"] == change.target))
            nodes = _node(output[SOURCE_PATHS[0]], ("routes",))
            if not isinstance(nodes, SequenceNode) or nodes.flow_style:
                raise ValueError("rate route requires a block sequence")
            parent = next(
                n
                for n in nodes.value
                if isinstance(n, MappingNode)
                and any(k.value == "route_id" and v.value == change.target for k, v in n.value)
            )
        elif change.scope == "provider" and change.metric in {"rpm", "tpm"}:
            block = limits["providers"].get(change.target)
            stamp = provider_rate_digest(load_config(texts[SOURCE_PATHS[0]]), change.provider)
            parent = _node(output[SOURCE_PATHS[0]], ("providers", change.target))
            if change.target != change.provider:
                raise ValueError("rate provider mismatch")
        else:
            raise ValueError("rate scope is not representable")
        if (
            not isinstance(block, dict)
            or stamp != change.config_digest
            or change.metric not in block
            or block[change.metric] != change.old
            or isinstance(change.old, bool)
            or not isinstance(change.old, (int, float))
            or change.old <= 0
            or isinstance(change.new, bool)
            or not isinstance(change.new, int)
            or change.new <= 0
            or (change.action == "tighten" and change.new >= change.old)
            or (change.action == "offer_increase" and change.new <= change.old)
            or change.action not in {"tighten", "offer_increase"}
        ):
            raise ValueError("rate scalar changed or invalid")
        if not isinstance(parent, MappingNode) or parent.flow_style:
            raise ValueError("rate edits require a block mapping")
        node = next((v for k, v in parent.value if k.value == change.metric), None)
        if not isinstance(node, ScalarNode) or node.tag not in {
            "tag:yaml.org,2002:int",
            "tag:yaml.org,2002:float",
        }:
            raise ValueError("rate field requires an explicit numeric scalar")
        text = output[SOURCE_PATHS[0]]
        output[SOURCE_PATHS[0]] = (
            text[: node.start_mark.index] + str(change.new) + text[node.end_mark.index :]
        )
        block[change.metric] = change.new
    if set(plan.removed_routes) & set(plan.paid_routes):
        raise ValueError("removal conflicts with paid edit")
    output[SOURCE_PATHS[0]] = _remove_routes(output[SOURCE_PATHS[0]], plan.removed_routes)
    limits["routes"] = [r for r in limits["routes"] if r.get("route_id") not in plan.removed_routes]
    if plan.lane_repairs and plan.backups:
        raise ValueError("lane repair conflicts with additions")
    for lane, models, backups, reasoning in plan.lane_repairs:
        block = site["llm_lanes"][lane]
        if not models:
            raise ValueError("lane repair cannot empty a lane")
        for field, value in (
            ("models", list(models)),
            ("backup_models", list(backups) if backups else None),
            ("reasoning", dict(reasoning) if reasoning else None),
        ):
            old = block.get(field)
            if old == value or (field not in block and value is None):
                continue
            output[SOURCE_PATHS[1]] = _set_lane_field(output[SOURCE_PATHS[1]], lane, field, value)
            if value is None:
                block.pop(field, None)
            else:
                block[field] = value
        if not backups and "backup_after_attempts" in block:
            output[SOURCE_PATHS[1]] = _set_lane_field(
                output[SOURCE_PATHS[1]], lane, "backup_after_attempts", None
            )
            block.pop("backup_after_attempts")
    for rid in plan.paid_routes:
        matches = [r for r in limits["routes"] if r.get("route_id") == rid]
        if len(matches) != 1 or not isinstance(matches[0].get("free"), bool):
            raise ValueError("paid route missing or unsupported")
        matches[0]["free"] = False
        output[SOURCE_PATHS[0]] = _set_route_free(output[SOURCE_PATHS[0]], rid)
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
    additions = []
    if (
        plan.acknowledged
        and "acknowledged" in decisions
        and not isinstance(decisions["acknowledged"], list)
    ):
        raise ValueError("acknowledged must be a list")
    for provider, model_glob, stamp in plan.acknowledged:
        if any(
            x.get("provider") == provider
            and x.get("model_glob") == model_glob
            and x.get("verdict") == "not_entitled"
            for x in decisions.get("acknowledged") or []
        ):
            continue
        entry = {
            "provider": provider,
            "model_glob": model_glob,
            "verdict": "not_entitled",
            "reason": "Maintainer /apply keep-paid selection",
            "decided_on": stamp,
        }
        decisions.setdefault("acknowledged", []).append(entry)
        additions.append(entry)
    output[SOURCE_PATHS[2]] = _append(output[SOURCE_PATHS[2]], ("acknowledged",), additions)
    for path in SOURCE_PATHS:
        if load_config(output[path]) != expected[path]:
            raise ValueError(f"unexpected semantic change in {path}")
    return output
