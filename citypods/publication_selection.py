"""Pure, reviewed publication projections; retained observations are never modified."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

from citypods import bodies
from citypods.records import source_key as city_source_key


@dataclass(frozen=True)
class PublicationMember:
    uid: str
    provider_guid: str
    record_fingerprint: str


@dataclass(frozen=True)
class PublicationGroup:
    id: str
    source_key: str
    identity_kind: str
    identity_key: str
    members: tuple[PublicationMember, ...]
    preferred_uid: str
    evidence_refs: tuple
    approval_ref: str
    exposure: Mapping
    search: bool
    date_resolution: Mapping | None
    feed_slug: str
    declaration: str


@dataclass(frozen=True)
class SelectionIndex:
    groups: tuple[PublicationGroup, ...]
    owners: Mapping


@dataclass(frozen=True)
class SelectionDiagnostic:
    code: str
    source_key: str
    group_id: str
    uid: str | None = None


@dataclass(frozen=True)
class PublicationPlan:
    held: bool
    diagnostics: tuple[SelectionDiagnostic, ...]
    selected_uids: tuple[str, ...]
    suppressed_uids: tuple[str, ...]
    policy_hash: str | None
    raw_items: tuple
    public_items: tuple
    owner_by_uid: Mapping


def _mapping(value, keys, context):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{context}: expected exactly {', '.join(sorted(keys))}")
    return value


def _text(value, context, pattern=None):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: expected nonempty string")
    if pattern and not re.fullmatch(pattern, value):
        raise ValueError(f"{context}: invalid format")
    return value


def _url(value, context):
    value = _text(value, context)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{context}: expected absolute HTTPS URL")
    return value


def _refs(value, context, *, empty=False):
    if not isinstance(value, list) or (not value and not empty):
        raise ValueError(f"{context}: expected nonempty list")
    result = []
    for ref in value:
        ref = _mapping(ref, ("url", "retrieved_at", "content_hash"), context)
        _url(ref["url"], context)
        timestamp = datetime.fromisoformat(
            _text(ref["retrieved_at"], context).replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError(f"{context}: timestamp must have timezone")
        _text(ref["content_hash"], context, "[0-9a-f]{64}")
        result.append(MappingProxyType(dict(ref)))
    return tuple(result)


def parse_publication_selection(raw, *, source_key, feed_slug):
    """Validate the exact v1 declaration without fetching evidence."""
    context = f"{feed_slug}: publication_selection"
    raw = _mapping(raw, ("version", "groups"), context)
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError(f"{context}: version must be integer 1")
    if not isinstance(raw["groups"], list):
        raise ValueError(f"{context}: groups must be list")
    groups = []
    keys = (
        "id",
        "source_key",
        "identity_kind",
        "identity_key",
        "members",
        "preferred_uid",
        "evidence_refs",
        "approval_ref",
        "exposure",
        "search",
        "date_resolution",
    )
    for value in raw["groups"]:
        value = _mapping(value, keys, context)
        ctx = f"{context}/{value['id']}"
        _text(value["id"], ctx, "[a-z0-9][a-z0-9-]{0,63}")
        _text(value["source_key"], ctx, "[0-9a-f]{12}")
        if value["source_key"] != source_key:
            raise ValueError(f"{ctx}: source key differs from declaring feed")
        if value["identity_kind"] not in ("same_provider_guid", "verified_granicus_clip"):
            raise ValueError(f"{ctx}: unsupported identity_kind")
        identity = _text(value["identity_key"], ctx)
        if value["identity_kind"] == "verified_granicus_clip":
            url = urlsplit(_url(identity, ctx))
            if (
                url.netloc != url.hostname
                or url.hostname != url.hostname.lower()
                or not re.fullmatch("/clip/[0-9]+", url.path)
                or url.query
                or url.fragment
            ):
                raise ValueError(f"{ctx}: invalid Granicus identity key")
        members_raw = value["members"]
        if not isinstance(members_raw, list) or len(members_raw) < 2:
            raise ValueError(f"{ctx}: at least two members required")
        members = []
        for member in members_raw:
            member = _mapping(member, ("uid", "provider_guid", "record_fingerprint"), ctx)
            members.append(
                PublicationMember(
                    _text(member["uid"], ctx, "[0-9a-f]{16}"),
                    _text(member["provider_guid"], ctx),
                    _text(member["record_fingerprint"], ctx, "[0-9a-f]{64}"),
                )
            )
        if len({m.uid for m in members}) != len(members):
            raise ValueError(f"{ctx}: duplicate member UID")
        _text(value["preferred_uid"], ctx, "[0-9a-f]{16}")
        if value["preferred_uid"] not in {m.uid for m in members}:
            raise ValueError(f"{ctx}: preferred_uid must be listed member")
        refs = _refs(value["evidence_refs"], ctx)
        approval = _url(value["approval_ref"], ctx)
        approved = urlsplit(approval)
        if approved.netloc != "github.com" or not re.fullmatch(
            r"/[^/]+/[^/]+/(issues|pull)/\d+", approved.path
        ):
            raise ValueError(f"{ctx}: approval_ref must be GitHub issue or PR")
        exposure = _mapping(value["exposure"], ("status", "artifacts", "rationale"), ctx)
        if exposure["status"] not in (
            "known-current",
            "both-published",
            "never-published",
            "historical-unknown",
        ):
            raise ValueError(f"{ctx}: invalid exposure status")
        artifacts = _refs(
            exposure["artifacts"],
            ctx,
            empty=exposure["status"] in ("never-published", "historical-unknown"),
        )
        _text(exposure["rationale"], ctx)
        if type(value["search"]) is not bool:
            raise ValueError(f"{ctx}: search must be boolean")
        resolution = value["date_resolution"]
        if resolution is not None:
            resolution = _mapping(resolution, ("official_date", "evidence_url"), ctx)
            _text(resolution["official_date"], ctx, r"\d{4}-\d{2}-\d{2}")
            date.fromisoformat(resolution["official_date"])
            if resolution["evidence_url"] not in {r["url"] for r in refs}:
                raise ValueError(f"{ctx}: date evidence must occur in evidence_refs")
            resolution = MappingProxyType(dict(resolution))
        canonical = dict(value)
        canonical["members"] = sorted(members_raw, key=lambda m: m["uid"])
        groups.append(
            PublicationGroup(
                value["id"],
                source_key,
                value["identity_kind"],
                identity,
                tuple(sorted(members, key=lambda m: m.uid)),
                value["preferred_uid"],
                refs,
                approval,
                MappingProxyType({**exposure, "artifacts": artifacts}),
                value["search"],
                resolution,
                feed_slug,
                json.dumps(canonical, sort_keys=True, separators=(",", ":")),
            )
        )
    return tuple(sorted(groups, key=lambda g: (g.source_key, g.id)))


def load_selection_index(cities):
    """Validate global overlaps and owning feeds before publication."""
    groups = []
    owners = {}
    for city in cities:
        owners[city.slug] = city
        if "publication_selection" in city.extra:
            groups.extend(
                parse_publication_selection(
                    city.extra["publication_selection"],
                    source_key=city_source_key(city),
                    feed_slug=city.slug,
                )
            )
    seen_id, seen_identity, seen_member = {}, {}, {}
    for group in groups:
        for table, key in (
            (seen_id, (group.source_key, group.id)),
            (seen_identity, (group.source_key, group.identity_key)),
        ):
            prior = table.get(key)
            if prior and (prior.declaration != group.declaration or group.search):
                raise ValueError(f"{group.feed_slug}/{group.id}: conflicting group declaration")
            table[key] = group
        for member in group.members:
            key = (group.source_key, member.uid)
            prior = seen_member.get(key)
            if prior and prior.declaration != group.declaration:
                raise ValueError(f"{group.feed_slug}/{group.id}: member overlaps another group")
            seen_member[key] = group
    return SelectionIndex(
        tuple(sorted(groups, key=lambda g: (g.source_key, g.id, g.feed_slug))),
        MappingProxyType(owners),
    )


def _recording_url(record):
    guid = bodies._provider_guid(record)
    if isinstance(guid, str) and guid.startswith("https://"):
        return guid
    return record.get("video_url")


def record_identity_fingerprint(source_key, record):
    """Hash exact retained official identity observations, without URL normalization."""
    values = {key: record.get(key) for key in ("uid", "body", "title", "published")}
    values.update(
        source_key=source_key,
        provider_guid=bodies._provider_guid(record),
        recording_url=_recording_url(record),
    )
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _clip(url):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(pairs)
    if (
        parsed.scheme != "https"
        or parsed.netloc != parsed.hostname
        or parsed.path != "/MediaPlayer.php"
        or parsed.fragment
        or len(query) != len(pairs)
        or not re.fullmatch("[0-9]+", query.get("clip_id", ""))
        or not re.fullmatch("[0-9]+", query.get("view_id", ""))
    ):
        return None
    return f"https://{parsed.hostname}/clip/{query['clip_id']}"


def _uid(item):
    return item.get("uid") if isinstance(item, Mapping) else item.uid


def _project(index, source, groups, items, records):
    raw = tuple(items)
    diagnostics = []
    suppressed = set()
    owners = {}
    for group in groups:

        def fail(code, uid=None, group_id=group.id):
            diagnostics.append(SelectionDiagnostic(code, source, group_id, uid))

        if not records:
            fail("missing-source")
        declared = {m.uid for m in group.members}
        for member in group.members:
            record = records.get(member.uid)
            if record is None:
                fail(
                    "missing-preferred" if member.uid == group.preferred_uid else "missing-member",
                    member.uid,
                )
                continue
            if record_identity_fingerprint(source, record) != member.record_fingerprint:
                fail("fingerprint-changed", member.uid)
            if record.get("uid") != member.uid:
                fail("identity-mismatch", member.uid)
            guid = bodies._provider_guid(record)
            if guid != member.provider_guid:
                fail("guid-mismatch", member.uid)
            identity = (
                guid
                if group.identity_kind == "same_provider_guid"
                else _clip(_recording_url(record))
            )
            if identity != group.identity_key:
                fail("identity-mismatch", member.uid)
            city = index.owners[group.feed_slug]
            if not bodies.record_matches_body(
                record,
                bodies.source_body_filter(city.source),
                bodies.source_body_inclusions(city.source),
            ):
                fail("owner-selector-mismatch", member.uid)
        for uid, record in records.items():
            identity = (
                bodies._provider_guid(record)
                if group.identity_kind == "same_provider_guid"
                else _clip(_recording_url(record))
            )
            if identity == group.identity_key and uid not in declared:
                fail("unexpected-member", uid)
        dates = {
            str(records[m.uid].get("published") or "")[:10]
            for m in group.members
            if m.uid in records
        }
        if len(dates) > 1 or group.date_resolution is not None:
            resolution = group.date_resolution
            preferred = records.get(group.preferred_uid, {})
            if (
                len(dates) <= 1
                or not resolution
                or str(preferred.get("published") or "")[:10] != resolution["official_date"]
            ):
                fail("unresolved-date-conflict", group.preferred_uid)
        suppressed.update(declared - {group.preferred_uid})
        owners[group.preferred_uid] = index.owners[group.feed_slug]
    policy_hash = None
    if groups:
        declarations = sorted({(g.feed_slug, g.declaration) for g in groups})
        policy_hash = hashlib.sha256(
            json.dumps(declarations, separators=(",", ":")).encode()
        ).hexdigest()
    public = raw if diagnostics else tuple(item for item in raw if _uid(item) not in suppressed)
    return PublicationPlan(
        bool(diagnostics),
        tuple(diagnostics),
        tuple(_uid(x) for x in public),
        tuple(sorted(suppressed)),
        policy_hash,
        raw,
        public,
        MappingProxyType(owners),
    )


def select_feed_publication(index, city, items, records):
    """Project one feed only after full retained-source proof validation."""
    groups = tuple(g for g in index.groups if g.feed_slug == city.slug)
    return _project(index, city_source_key(city), groups, items, records)


def select_search_publication(index, source_key, records):
    """Project reviewed search owners, retaining record objects and ordering."""
    groups = tuple(g for g in index.groups if g.source_key == source_key and g.search)
    return _project(index, source_key, groups, records.values(), records)
