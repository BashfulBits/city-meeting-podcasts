"""Strict, source-local reviewed policy foundation; no activation or evaluation IO."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from citypods.bodies import body_key, matches, matches_exact_body_label, source_body_inclusions
from citypods.config import (
    _policy_approval as _approval,
)
from citypods.config import (
    _policy_slug as _slug,
)
from citypods.config import (
    _policy_strings as _strings,
)
from citypods.config import (
    validate_declaration,
)
from citypods.records import source_key as recording_source_key

FAMILIES = {"tif", "pid", "bond", "charter", "redistricting", "public_input", "public_briefings"}
PROOFS = {
    "official_body_identity",
    "recording_is_public_meeting",
    "same_source_namespace",
    "complete_available_history",
    "reviewed_positive_negative_cases",
}
EXCLUSIONS = {
    "promotional",
    "ceremony",
    "staff_training",
    "municipal_tv_show",
    "topic_only",
    "other_body",
}
MIGRATIONS = {"source_namespace", "uid", "official_metadata", "archived_records", "audio_artifacts"}
TRANSFORMS = {
    "normalized_exact_label",
    "reviewed_label_alias",
    "reviewed_recording_inclusion",
    "approved_family_marker",
}
SELECTORS = {"body", "body_any", "body_exact", "body_includes"}


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CitySource(StrictModel):
    city: str
    source_key: str

    @model_validator(mode="after")
    def valid(self):
        _slug(self.city)
        _slug(self.source_key)
        return self


class PolicyTemplate(StrictModel):
    id: str
    version: StrictInt = Field(gt=0)
    approval_ref: str
    scope: Literal["cross_city", "city_source"]
    city_source: CitySource | None
    identity_type: Literal["aggregate_family", "named_body"]
    aggregate_family: str | None
    permitted_transformations: tuple[str, ...]
    selector_forms: tuple[str, ...]
    official_proof_requirements: tuple[str, ...]
    exclusion_boundaries: tuple[str, ...]
    migration_boundaries: tuple[str, ...]
    positive_case_ids: tuple[str, ...]
    negative_case_ids: tuple[str, ...]
    transfer_case_ids: tuple[str, ...]

    @field_validator(
        "permitted_transformations",
        "selector_forms",
        "official_proof_requirements",
        "exclusion_boundaries",
        "migration_boundaries",
        "positive_case_ids",
        "negative_case_ids",
        "transfer_case_ids",
        mode="before",
    )
    @classmethod
    def immutable_lists(cls, value):
        _strings(value, nonempty=False)
        return tuple(value)

    @model_validator(mode="after")
    def valid(self):
        _slug(self.id)
        _approval(self.approval_ref)
        if (self.scope == "city_source") != (self.city_source is not None):
            raise ValueError("template scope/binding mismatch")
        if self.identity_type == "aggregate_family":
            if self.aggregate_family not in FAMILIES:
                raise ValueError("aggregate template needs an approved family")
        elif self.aggregate_family is not None:
            raise ValueError("named_body template cannot declare a family")
        for name in (
            "permitted_transformations",
            "selector_forms",
            "official_proof_requirements",
            "exclusion_boundaries",
            "migration_boundaries",
            "positive_case_ids",
            "negative_case_ids",
            "transfer_case_ids",
        ):
            _strings(
                list(getattr(self, name)),
                nonempty=name != "transfer_case_ids" or self.scope == "cross_city",
            )
        for name, allowed in (
            ("permitted_transformations", TRANSFORMS),
            ("selector_forms", SELECTORS),
            ("official_proof_requirements", PROOFS | {"exact_provider_guid"}),
            ("exclusion_boundaries", EXCLUSIONS),
            ("migration_boundaries", MIGRATIONS),
        ):
            if not set(getattr(self, name)) <= allowed:
                raise ValueError(f"unsupported {name}")
        if not PROOFS <= set(self.official_proof_requirements) or (
            set(self.exclusion_boundaries) != EXCLUSIONS
            or set(self.migration_boundaries) != MIGRATIONS
        ):
            raise ValueError("missing proof/exclusion/migration boundary")
        if "reviewed_recording_inclusion" in self.permitted_transformations and (
            "exact_provider_guid" not in self.official_proof_requirements
            or "body_includes" not in self.selector_forms
        ):
            raise ValueError("GUID inclusion needs exact proof and selector")
        if (
            "approved_family_marker" in self.permitted_transformations
            and self.aggregate_family != "tif"
        ):
            raise ValueError("family marker is supported only for TIF")
        if set(self.positive_case_ids) & set(self.negative_case_ids):
            raise ValueError("positive and negative cases overlap")
        return self


class IdentityBinding(StrictModel):
    identity_name: str
    evidence_ref_ids: list[str]
    recording_guids: list[str]


class Completeness(StrictModel):
    status: Literal["complete_available", "incomplete", "unknown"]
    evidence_ref_ids: list[str]
    exception_refs: list[str]


class OfficialEvidence(StrictModel):
    schema_version: StrictInt
    city: str
    source_key: str
    approval_ref: str
    policy_id: str
    policy_version: StrictInt = Field(gt=0)
    template_id: str
    template_version: StrictInt = Field(gt=0)
    identity_bindings: list[IdentityBinding]
    evidence_refs: list[Any]
    recordings: list[Any]
    completeness: Completeness
    positive_case_ids: list[str]
    negative_case_ids: list[str]
    transfer_case_ids: list[str]

    @model_validator(mode="after")
    def shared_values(self):
        # Lazy import preserves the shared P1 schemas without a config import cycle.
        from citypods.remedy_evaluation import EvidenceRef, Recording

        object.__setattr__(
            self, "evidence_refs", [EvidenceRef.model_validate(v) for v in self.evidence_refs]
        )
        object.__setattr__(
            self, "recordings", [Recording.model_validate(v) for v in self.recordings]
        )
        if self.schema_version != 1:
            raise ValueError("unsupported evidence schema version")
        _slug(self.city)
        _slug(self.source_key)
        _slug(self.policy_id)
        _slug(self.template_id)
        _approval(self.approval_ref)
        for key in ("positive_case_ids", "negative_case_ids", "transfer_case_ids"):
            _strings(getattr(self, key), nonempty=key != "transfer_case_ids")
        if set(self.positive_case_ids) & set(self.negative_case_ids):
            raise ValueError("positive and negative cases overlap")
        return self


def _ordered_hash(value):
    if isinstance(value, dict):
        return {k: _ordered_hash(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return sorted(
            (_ordered_hash(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True)
        )
    return value


@dataclass(frozen=True)
class TemplateIndex:
    entries: tuple[PolicyTemplate, ...] = ()
    hashes: tuple[tuple[str, int, str], ...] = ()

    @property
    def by_revision(self):
        return MappingProxyType({(t.id, t.version): t for t in self.entries})

    def get(self, template_id, version):
        return self.by_revision.get((template_id, version))


@dataclass(frozen=True)
class PolicyInstance:
    policy_id: str
    version: int | None
    status: str
    owner_slug: str
    city_slug: str
    source_key: str
    aggregate_family: str | None
    member_names: tuple[str, ...]
    identity_names: tuple[str, ...]
    approval_ref: str | None
    positive_case_ids: tuple[str, ...]
    negative_case_ids: tuple[str, ...]
    template_id: str | None
    template_version: int | None
    template_hash: str | None
    local_declaration_hash: str
    evidence_refs: tuple[str, ...] = ()
    proof_diagnostics: tuple[str, ...] = ()

    def declaration(self):
        return {
            "aggregate_family": self.aggregate_family,
            "member_names": list(self.member_names),
            "identity_names": list(self.identity_names),
        }


@dataclass(frozen=True)
class PolicyIndex:
    instances: tuple[PolicyInstance, ...] = ()

    @property
    def by_source(self):
        return MappingProxyType(
            {
                key: tuple(p for p in self.instances if p.source_key == key)
                for key in sorted({p.source_key for p in self.instances})
            }
        )


@dataclass(frozen=True)
class PolicyInstances:
    resolved: tuple[PolicyInstance, ...] = ()
    unresolved: tuple[PolicyInstance, ...] = ()


@dataclass(frozen=True)
class OwnershipResolution:
    status: str
    owner_slugs: tuple[str, ...]
    policy_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    holding_owner_slugs: tuple[str, ...]
    reasons: tuple[str, ...]


def load_policy_templates(config):
    from citypods.remedy_evaluation import RemedyConfig

    config = config if isinstance(config, RemedyConfig) else RemedyConfig.model_validate(config)
    entries = tuple(sorted(config.policy_templates, key=lambda t: (t.id, t.version)))
    if len({(t.id, t.version) for t in entries}) != len(entries):
        raise ValueError("duplicate policy template revision")
    return TemplateIndex(
        entries,
        tuple((t.id, t.version, canonical_hash(_ordered_hash(t.model_dump()))) for t in entries),
    )


def _declaration_instance(
    declaration,
    owner_slug,
    city_slug,
    source_key,
    templates=None,
    *,
    status=None,
    refs=(),
    diagnostics=(),
):
    declaration = validate_declaration(declaration, owner_slug)
    template = (
        templates.get(declaration.get("template_id"), declaration.get("template_version"))
        if templates
        else None
    )
    default_status = (
        "unresolved"
        if "template_id" in declaration
        else ("approved" if "policy_id" in declaration else "legacy_unversioned")
    )
    return PolicyInstance(
        declaration.get("policy_id", "legacy:" + owner_slug),
        declaration.get("version"),
        status or default_status,
        owner_slug,
        city_slug,
        source_key,
        declaration.get("aggregate_family"),
        tuple(declaration.get("member_names", [])),
        tuple(declaration.get("identity_names", [])),
        declaration.get("approval_ref"),
        tuple(sorted(declaration.get("positive_case_ids", []))),
        tuple(sorted(declaration.get("negative_case_ids", []))),
        declaration.get("template_id"),
        declaration.get("template_version"),
        canonical_hash(_ordered_hash(template.model_dump())) if template else None,
        canonical_hash(_ordered_hash(declaration)),
        tuple(sorted(refs)),
        tuple(sorted(diagnostics)),
    )


def _instance(city, templates=None, **kwargs):
    return _declaration_instance(
        city.extra["remedy_policy"],
        city.slug,
        city.city_entity or city.slug,
        recording_source_key(city),
        templates,
        **kwargs,
    )


def load_policies(feed_paths, *, templates=None):
    if not feed_paths:
        return PolicyIndex()
    paths = {slug: Path(path).resolve() for slug, path in feed_paths.items()}
    roots = {p.parent.parent for p in paths.values()}
    if len(roots) != 1 or any(p.parent.name != "feeds" for p in paths.values()):
        raise ValueError("feed policies require one shared config root")
    from citypods.config import load_city_configs

    cities = {city.slug: city for city in load_city_configs(next(iter(roots)), {})}
    instances = []
    for slug, path in sorted(paths.items()):
        if slug not in cities or path.stem != slug:
            raise ValueError(f"{slug}: feed path/model slug mismatch")
        city = cities[slug]
        if "remedy_policy" in city.extra:
            instances.append(_instance(city, templates))
    return PolicyIndex(tuple(instances))


def instantiate_policies(city, source_key, templates, official_evidence):
    if "remedy_policy" not in city.extra:
        return PolicyInstances()

    def unresolved(reason):
        return PolicyInstances(
            unresolved=(_instance(city, templates, status="unresolved", diagnostics=(reason,)),)
        )

    if source_key != recording_source_key(city):
        return unresolved("source namespace mismatch")
    declaration = validate_declaration(city.extra["remedy_policy"], city.slug)
    template = templates.get(declaration.get("template_id"), declaration.get("template_version"))
    if template is None:
        return unresolved("unknown template revision")
    try:
        packet = (
            official_evidence
            if isinstance(official_evidence, OfficialEvidence)
            else OfficialEvidence.model_validate(official_evidence)
        )
    except ValueError:
        return unresolved("invalid official evidence packet")
    expected = (
        city.city_entity or city.slug,
        source_key,
        declaration.get("policy_id"),
        declaration.get("version"),
        template.id,
        template.version,
        declaration.get("approval_ref"),
    )
    actual = (
        packet.city,
        packet.source_key,
        packet.policy_id,
        packet.policy_version,
        packet.template_id,
        packet.template_version,
        packet.approval_ref,
    )
    if actual != expected:
        return unresolved("packet identity/approval mismatch")
    if (
        template.city_source
        and (template.city_source.city, template.city_source.source_key) != expected[:2]
    ):
        return unresolved("template city/source binding mismatch")
    if template.aggregate_family != declaration.get("aggregate_family"):
        return unresolved("template identity type mismatch")
    for key in ("positive_case_ids", "negative_case_ids"):
        if set(getattr(packet, key)) != set(declaration.get(key, [])) or set(
            getattr(packet, key)
        ) != set(getattr(template, key)):
            return unresolved("case reference mismatch")
    if set(packet.transfer_case_ids) != set(template.transfer_case_ids):
        return unresolved("transfer case reference mismatch")
    refs = {ref.id for ref in packet.evidence_refs}
    recordings = {record.provider_guid: record for record in packet.recordings}
    if any(
        not ref.id.strip() or not ref.url.strip() or not ref.source_span.strip()
        for ref in packet.evidence_refs
    ):
        return unresolved("empty official evidence span")
    if len(refs) != len(packet.evidence_refs) or len(recordings) != len(packet.recordings):
        return unresolved("duplicate evidence/recording identity")
    complete = packet.completeness
    try:
        _strings(complete.evidence_ref_ids)
        _strings(complete.exception_refs, nonempty=False)
    except ValueError:
        return unresolved("invalid completeness references")
    if (
        complete.status != "complete_available"
        or not complete.evidence_ref_ids
        or not set(complete.evidence_ref_ids + complete.exception_refs) <= refs
    ):
        return unresolved("incomplete or ungrounded available history")
    names = set(declaration.get("identity_names", []))
    bindings = {binding.identity_name: binding for binding in packet.identity_bindings}
    if not names or set(bindings) != names or len(bindings) != len(packet.identity_bindings):
        return unresolved("missing or contradictory identity bindings")
    for name, binding in bindings.items():
        try:
            _strings(binding.evidence_ref_ids)
            _strings(binding.recording_guids)
        except ValueError:
            return unresolved("invalid identity binding references")
        if (
            not binding.evidence_ref_ids
            or not set(binding.evidence_ref_ids) <= refs
            or not binding.recording_guids
            or not set(binding.recording_guids) <= recordings.keys()
        ):
            return unresolved("missing evidence reference or recording binding")
        if any(recordings[guid].body != name for guid in binding.recording_guids):
            return unresolved("recording official identity mismatch")
    try:
        included = {item.provider_guid for item in source_body_inclusions(city.source)}
    except ValueError:
        return unresolved("invalid recording inclusion declaration")
    if included and (
        "reviewed_recording_inclusion" not in template.permitted_transformations
        or not included <= recordings.keys()
    ):
        return unresolved("unsupported or ungrounded recording inclusion")
    forms = {key for key in SELECTORS if key in city.source}
    if not forms <= set(template.selector_forms):
        return unresolved("unsupported selector form")
    return PolicyInstances(resolved=(_instance(city, templates, status="approved", refs=refs),))


def _matches_tif_family(value: str, policy: dict[str, Any]) -> bool:
    """Conservative policy clues, including reviewed names whose provider labels omit TIF."""
    normalized = body_key(value)
    tokens = set(normalized.split())
    if tokens & {"tif", "tirz"} or any(
        " " + phrase + " " in " " + normalized + " "
        for phrase in ("tax increment", "reinvestment zone")
    ):
        return True
    names = policy.get("member_names", [])
    return isinstance(names, list) and any(
        isinstance(name, str) and name.strip() and matches(value, name) for name in names
    )


def _policy_identity_matches(value: str, policy: dict[str, Any]) -> bool:
    """Exact reviewed labels; normalization does not turn a topic into an owning body."""
    return any(matches_exact_body_label(value, name) for name in policy.get("identity_names", []))


def _policy_family_clue(value: str, policy: dict[str, Any]) -> bool:
    """Markers can hold recreation proposals but do not approve new ownership."""
    family = policy.get("aggregate_family")
    if family == "tif":
        return _matches_tif_family(value, policy) or _policy_identity_matches(value, policy)
    normalized = " " + body_key(value) + " "
    markers = {
        "pid": ("pid", "public improvement district"),
        "bond": ("bond",),
        "charter": ("charter",),
        "redistricting": ("redistricting",),
        "public_input": (
            "town hall",
            "townhall",
            "public input",
            "public meeting",
            "public hearing",
            "public forum",
            "community meeting",
            "neighborhood meeting",
        ),
        "public_briefings": (
            "press conference",
            "news conference",
            "public presentation",
            "public briefing",
        ),
    }
    return (
        any(" " + body_key(marker) + " " in normalized for marker in markers.get(family, ()))
        or _policy_identity_matches(value, policy)
        or any(
            matches(value, name)
            for name in policy.get("member_names", []) + policy.get("identity_names", [])
        )
    )


def _policy_verified_owner(value: str, policy: dict[str, Any]) -> bool:
    if _policy_identity_matches(value, policy):
        return True
    # Preserve the reviewed TIF marker rule. Topic-bearing proceedings of another body need
    # explicit identity evidence, even if the family marker would otherwise match.
    if policy.get("aggregate_family") != "tif":
        return False
    tokens = set(body_key(value).split())
    if tokens & {
        "council",
        "training",
        "announcement",
        "promo",
        "promotion",
        "ceremony",
        "conference",
        "discussion",
        "presentation",
        "television",
        "show",
    }:
        return False
    return _matches_tif_family(value, {})


def resolve_owner(label, source_key, policies):
    instances = (
        policies.instances
        if isinstance(policies, PolicyIndex)
        else policies.resolved + policies.unresolved
    )
    local = [p for p in instances if p.source_key == source_key]
    usable = [p for p in local if p.status != "unresolved"]
    exact = [p for p in usable if _policy_identity_matches(label, p.declaration())]
    owners = exact or [p for p in usable if _policy_verified_owner(label, p.declaration())]
    holding = [p for p in local if _policy_family_clue(label, p.declaration())]
    contradictions = [
        p
        for p in local
        if p.status == "unresolved" and _policy_identity_matches(label, p.declaration())
    ]
    status = "ambiguous" if contradictions else "verified" if owners else "unknown"
    if contradictions:
        owners = []
    return OwnershipResolution(
        status,
        tuple(sorted({p.owner_slug for p in owners})),
        tuple(sorted({p.policy_id for p in owners})),
        tuple(sorted({ref for p in owners for ref in p.evidence_refs})),
        tuple(sorted({p.owner_slug for p in holding})),
        tuple(sorted({reason for p in contradictions for reason in p.proof_diagnostics})),
    )


@dataclass(frozen=True)
class CoverageRow:
    source_key: str
    uid: str | None
    provider_guid: str | None
    body: Any
    title: Any
    date: Any
    observation_refs: tuple[str, ...]
    configured_owner_slugs: tuple[str, ...]
    verified_owner_slugs: tuple[str, ...]
    holding_owner_slugs: tuple[str, ...]
    policy_ids: tuple[str, ...]
    policy_statuses: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]
    status: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class CoverageReplay:
    rows: tuple[CoverageRow, ...]
    totals: tuple[tuple[str, int], ...]


def material_evidence_hash(evidence):
    """Hash supplied material, with collection order and report clocks excluded."""

    def canonical(value):
        if isinstance(value, dict):
            return {
                key: canonical(item) for key, item in sorted(value.items()) if key != "observed_at"
            }
        if isinstance(value, (list, tuple)):
            return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item))
        return value

    return canonical_hash(canonical(evidence))


def replay_coverage(recordings, feeds, policies=None):
    """Replay every supplied observation without guessing identity or editing assignments."""
    from citypods.bodies import record_matches_body, source_body_filter

    policies = policies or PolicyIndex()
    feeds = tuple(feeds)
    instances = (
        policies.instances
        if isinstance(policies, PolicyIndex)
        else policies.resolved + policies.unresolved
    )
    observations = [dict(item) for item in recordings]
    guid_uids: dict[tuple[str, str], set[str]] = {}
    for item in observations:
        if item.get("uid") and item.get("provider_guid"):
            guid_uids.setdefault((item["source_key"], item["provider_guid"]), set()).add(
                item["uid"]
            )
    grouped: dict[tuple, list[dict]] = {}
    for number, item in enumerate(observations):
        source = item["source_key"]
        uid = item.get("uid")
        guid = item.get("provider_guid")
        known = guid_uids.get((source, guid), set())
        if not uid and len(known) == 1:
            uid = next(iter(known))
        item["uid"] = uid
        identity = (
            ("uid", uid)
            if uid
            else ("guid", guid)
            if guid and len(known) < 2
            else ("unknown", number)
        )
        grouped.setdefault((source, *identity), []).append(item)
    rows = []
    for identity, items in grouped.items():
        variants: dict[str, list[dict]] = {}
        for item in items:
            key = json.dumps(
                {key: item.get(key) for key in ("provider_guid", "body", "title", "date")},
                sort_keys=True,
            )
            variants.setdefault(key, []).append(item)
        for copies in variants.values():
            item = copies[0]
            source, label = item["source_key"], item.get("body")
            diagnostics = []
            if len(variants) > 1:
                diagnostics.append("identity-conflict")
            if identity[1] == "unknown":
                diagnostics.append("uniqueness-unknown")
            if not isinstance(label, str) or not label.strip():
                diagnostics.append("missing-or-malformed-label")
            owners = []
            for feed in feeds:
                if recording_source_key(feed) != source:
                    continue
                try:
                    selector = source_body_filter(feed.source)
                    inclusions = source_body_inclusions(feed.source)
                    safe_item = {**item, "body": label if isinstance(label, str) else None}
                    if record_matches_body(safe_item, selector, inclusions):
                        owners.append(feed.slug)
                except ValueError:
                    diagnostics.append("invalid-feed-selector:" + feed.slug)
            resolution = resolve_owner(label if isinstance(label, str) else "", source, policies)
            configured = tuple(sorted(set(owners)))
            if configured != resolution.owner_slugs:
                diagnostics.append("configured-verified-owners-differ")
            status = (
                "ambiguous"
                if "identity-conflict" in diagnostics or resolution.status == "ambiguous"
                else "selected"
                if configured
                else "unknown"
            )
            rows.append(
                CoverageRow(
                    source,
                    item.get("uid"),
                    item.get("provider_guid"),
                    label,
                    item.get("title"),
                    item.get("date"),
                    tuple(
                        sorted({ref for copy in copies for ref in copy.get("observation_refs", [])})
                    ),
                    configured,
                    resolution.owner_slugs,
                    resolution.holding_owner_slugs,
                    resolution.policy_ids,
                    tuple(
                        sorted((p.policy_id, p.status) for p in instances if p.source_key == source)
                    ),
                    resolution.evidence_refs,
                    status,
                    tuple(sorted(set(diagnostics))),
                )
            )
    rows.sort(key=lambda row: json.dumps(row.__dict__, sort_keys=True))
    totals = {"observations": len(observations), "rows": len(rows)}
    totals.update(
        {
            status: sum(row.status == status for row in rows)
            for status in ("selected", "unknown", "ambiguous")
        }
    )
    totals["verified_policy"] = sum(bool(row.verified_owner_slugs) for row in rows)
    return CoverageReplay(tuple(rows), tuple(sorted(totals.items())))
