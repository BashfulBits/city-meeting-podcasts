"""Strict, source-local reviewed policy foundation; no activation or evaluation IO."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from citypods.bodies import body_key, matches, matches_exact_body_label
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


def _slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise ValueError("invalid policy slug")


def _approval(value):
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not re.fullmatch(r"/[^/]+/[^/]+/(issues|pull)/[0-9]+", parsed.path)
        or parsed.query
        or (
            parsed.fragment
            and not re.fullmatch(
                r"(issuecomment|discussion_r|pullrequestreview)-[0-9]+", parsed.fragment
            )
        )
    ):
        raise ValueError("approval_ref must be an HTTPS GitHub issue/PR/comment URL")


def _strings(value, *, nonempty=True, distinct=True):
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or (distinct and len(value) != len(set(value)))
    ):
        raise ValueError("expected distinct nonempty strings")


def validate_declaration(value, context="feed"):
    try:
        if not isinstance(value, dict) or set(value) - {
            "aggregate_family",
            "member_names",
            "identity_names",
            "policy_id",
            "version",
            "positive_case_ids",
            "negative_case_ids",
            "approval_ref",
            "template_id",
            "template_version",
        }:
            raise ValueError("unknown policy declaration keys")
        family = value.get("aggregate_family")
        if "aggregate_family" in value and (not isinstance(family, str) or family not in FAMILIES):
            raise ValueError("unknown aggregate family")
        if not family and not value.get("identity_names"):
            raise ValueError("identity-only policy requires identity_names")
        for key in ("member_names", "identity_names", "positive_case_ids", "negative_case_ids"):
            if key in value:
                _strings(
                    value[key],
                    nonempty=key.endswith("case_ids") or (key == "identity_names" and not family),
                    distinct=key.endswith("case_ids"),
                )
                if key.endswith("names") and any(not body_key(n) for n in value[key]):
                    raise ValueError("empty normalized official name")
        if any(c in name for name in value.get("identity_names", []) for c in "*?"):
            raise ValueError("identity_names cannot contain wildcards")
        for keys in (("policy_id", "version", "approval_ref"), ("template_id", "template_version")):
            present = [key in value for key in keys]
            if any(present) and not all(present):
                raise ValueError("incomplete provenance tuple")
        for key in ("policy_id", "template_id"):
            if key in value:
                _slug(value[key])
        for key in ("version", "template_version"):
            if key in value and (type(value[key]) is not int or value[key] < 1):
                raise ValueError("version must be a strict positive integer")
        if "approval_ref" in value:
            _approval(value["approval_ref"])
        if "template_id" in value and "policy_id" not in value:
            raise ValueError("template reference requires reviewed provenance")
        if set(value.get("positive_case_ids", [])) & set(value.get("negative_case_ids", [])):
            raise ValueError("positive and negative cases overlap")
        return value
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{context}: invalid remedy_policy: {exc}") from exc


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
    included = {item["provider_guid"] for item in city.source.get("body_includes", [])}
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
