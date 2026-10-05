"""Human-approved archive-only projections without changes to retained observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType

from citypods.records import source_key

_FIELDS = frozenset({"uid", "provider_guid", "reason", "approval_ref"})


def parse_archive_only(raw):
    """Validate exact declarations, retaining their original identity and approval text."""
    if not isinstance(raw, list):
        raise ValueError("archive_only: expected list")
    declarations = {}
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) != _FIELDS:
            raise ValueError(
                "archive_only: expected exactly uid, provider_guid, reason, approval_ref"
            )
        for key, value in entry.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"archive_only.{key}: expected nonempty string")
        declaration = dict(entry)
        prior = declarations.get(entry["uid"])
        if prior is not None and prior != declaration:
            raise ValueError(f"archive_only: conflicting declaration for UID {entry['uid']}")
        declarations[entry["uid"]] = declaration
    return tuple(MappingProxyType(declarations[uid]) for uid in sorted(declarations))


def load_archive_index(cities):
    """Combine feed declarations into an immutable index scoped to each retained source."""
    sources = {}
    for city in cities:
        if "archive_only" not in city.extra:
            continue
        declarations = parse_archive_only(city.extra["archive_only"])
        if not declarations:
            continue
        key = source_key(city)
        source = sources.setdefault(key, {})
        for declaration in declarations:
            uid = declaration["uid"]
            prior = source.get(uid)
            if prior is not None and prior != declaration:
                raise ValueError(
                    f"archive_only: conflicting declaration for source {key}, UID {uid}"
                )
            source[uid] = declaration
    return MappingProxyType(
        {key: MappingProxyType(source) for key, source in sorted(sources.items())}
    )


def archive_policy_hash(index, source):
    """Return a deterministic cache input, or None when the source has no disposition."""
    declarations = index.get(source)
    if not declarations:
        return None
    payload = {uid: dict(value) for uid, value in sorted(declarations.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def is_archive_only(index, source, record_or_episode):
    """Check both stored identities; a conflicting GUID fails instead of hiding a record."""
    if isinstance(record_or_episode, Mapping):
        uid = record_or_episode.get("uid")
        guid = record_or_episode.get("provider_guid")
    else:
        uid = record_or_episode.uid
        guid = record_or_episode.guid
    declaration = index.get(source, {}).get(uid)
    if declaration is None:
        return False
    if declaration["provider_guid"] != guid:
        raise ValueError(f"archive_only: provider GUID mismatch for source {source}, UID {uid}")
    return True
