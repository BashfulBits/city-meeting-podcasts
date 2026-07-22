"""Load global site config and per-city YAML into validated models."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from citypods.models import City, FeedLifecycle
from citypods.ops.workqueue import BacklogPolicy
from citypods.providers import get_provider
from citypods.security import validate_city_sources, validate_source_url

# Keys that must be present AND non-empty.
REQUIRED_CITY_KEYS = (
    "slug",
    "provider",
    "source",
    "podcast_title",
    "podcast_author",
    "podcast_description",
)
# podcast_email is required by the RSS spec but many cities publish no public
# address; the key must exist but a blank value is allowed through (see PLAN.md).
PRESENT_BUT_MAY_BE_BLANK = ("podcast_email",)

# slug/aliases feed directly into output_dir / city.slug (run.py) — the same lowercase
# alphanumeric-plus-hyphen shape scripts/generate_board_cities.py's slugify() always produces.
# Rejecting anything else up front (CR2-CP-49) closes a path-traversal footgun for a trusted-but-
# fallible config author (a "../.."-laden or absolute-path slug is not a realistic operator
# input, but the guarantee should not depend on every author getting it right by hand).
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_EPISODE_UID_RE = re.compile(r"^[0-9a-f]{16}$")
_LIFECYCLE_STATUSES = frozenset({"active", "paused", "dormant", "retired"})


def _validate_slug_format(slug: str, *, source_file: Path, kind: str) -> None:
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"{source_file.name}: {kind} {slug!r} must be lowercase alphanumeric segments "
            "joined by single hyphens (matches ^[a-z0-9]+(-[a-z0-9]+)*$)"
        )


def _validate_asr_workers(asr_workers: int, *, source_file: Path) -> int:
    """``stages.py`` divides ``cpu_count() / city.asr_workers`` to size per-inference thread
    pools; an operator-set ``asr_workers: 0`` reached that division at runtime mid-shard as a
    ZeroDivisionError instead of failing at config load (M1/CR2-CP-43)."""
    if asr_workers < 1:
        raise ValueError(f"{source_file.name}: asr_workers must be >= 1, got {asr_workers}")
    return asr_workers


def _parse_source_id(raw: object, *, source_file: Path) -> str | None:
    if raw is None:
        return None
    source_id = str(raw)
    if not _SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError(
            f"{source_file.name}: source_id {source_id!r} must be 1-64 lowercase "
            "alphanumeric/hyphen characters and may not start with a hyphen"
        )
    return source_id


def _parse_uid_overrides(raw: object, *, source_file: Path) -> dict[str, str]:
    """Validate reviewed replacement-provider GUID -> stable UID migration joins."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{source_file.name}: uid_overrides must be a mapping")
    overrides: dict[str, str] = {}
    targets: dict[str, str] = {}
    for raw_guid, raw_uid in raw.items():
        guid = str(raw_guid).strip()
        uid = str(raw_uid).strip()
        if not guid or len(guid) > 256 or any(ch in guid for ch in "\r\n"):
            raise ValueError(f"{source_file.name}: uid_overrides contains an invalid provider GUID")
        if not _EPISODE_UID_RE.fullmatch(uid):
            raise ValueError(
                f"{source_file.name}: uid_overrides[{guid!r}] must be a 16-character "
                "lowercase hexadecimal stable UID"
            )
        prior_guid = targets.get(uid)
        if prior_guid is not None and prior_guid != guid:
            raise ValueError(
                f"{source_file.name}: uid_overrides maps both {prior_guid!r} and {guid!r} "
                f"to stable UID {uid!r}"
            )
        overrides[guid] = uid
        targets[uid] = guid
    return overrides


def _parse_lifecycle(raw: object, *, source_file: Path) -> FeedLifecycle:
    if raw is None:
        return FeedLifecycle()
    if not isinstance(raw, dict):
        raise ValueError(f"{source_file.name}: lifecycle must be a mapping")

    unknown = set(raw) - {"status", "recheck_after", "reason", "evidence_url"}
    if unknown:
        raise ValueError(
            f"{source_file.name}: lifecycle has unknown keys: {', '.join(sorted(unknown))}"
        )
    status = str(raw.get("status") or "")
    if status not in _LIFECYCLE_STATUSES:
        raise ValueError(
            f"{source_file.name}: lifecycle.status must be one of "
            f"{', '.join(sorted(_LIFECYCLE_STATUSES))}, got {status!r}"
        )

    reason = str(raw.get("reason") or "").strip()
    recheck_raw = raw.get("recheck_after")
    recheck_after: date | None = None
    if recheck_raw is not None:
        try:
            recheck_after = date.fromisoformat(str(recheck_raw))
        except ValueError as exc:
            raise ValueError(
                f"{source_file.name}: lifecycle.recheck_after must be YYYY-MM-DD"
            ) from exc

    if status == "paused" and recheck_after is None:
        raise ValueError(f"{source_file.name}: paused lifecycle requires recheck_after")
    if status != "paused" and recheck_after is not None:
        raise ValueError(
            f"{source_file.name}: lifecycle.recheck_after is allowed only for paused feeds"
        )
    if status != "active" and not reason:
        raise ValueError(f"{source_file.name}: non-active lifecycle requires a reason")
    if status == "active" and reason:
        raise ValueError(f"{source_file.name}: active lifecycle may not carry a reason")

    evidence_raw = raw.get("evidence_url")
    evidence_url = str(evidence_raw).strip() if evidence_raw is not None else None
    if evidence_url:
        validate_source_url(evidence_url, resolve=False)

    return FeedLifecycle(
        status=status,
        recheck_after=recheck_after,
        reason=reason,
        evidence_url=evidence_url or None,
    )


def load_site_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    data.setdefault("defaults", {})
    # Search is a site-wide render feature, not a per-feed provider setting.  Keep the opt-out
    # explicit so a deployment that disables it also has a deterministic default.
    data["defaults"].setdefault("search", True)
    # Search reads bounded, restartable sidecars during render.  Keep that optional work from
    # turning the deploy-only phase into an unbounded job; zero deliberately disables this cap.
    data["defaults"].setdefault("search_index_budget_minutes", 20)
    return data


def load_backlog_policy(site_config: dict) -> BacklogPolicy:
    """Build the H5 backlog prioritization policy from ``backlog_priority`` / ``city_order``
    in site config. Empty/absent ⇒ the behavior-preserving identity order."""
    return BacklogPolicy.from_site_config(site_config)


def load_entity_configs(entities_dir: str | Path) -> dict[str, dict]:
    """Load ``config/cities/<slug>.yml`` entity files into a slug→raw-dict map.

    Entity files supply city-level fields (``city_website``, ``meetings_url``,
    ``state``, ``colors``) that are shared across every feed for the same entity.
    Feed YAMLs reference an entity via the ``city:`` key; explicit feed-level values
    override the entity values."""
    entities_dir = Path(entities_dir)
    entities: dict[str, dict] = {}
    if not entities_dir.is_dir():
        return entities
    for path in sorted(entities_dir.glob("*.yml")):
        if path.name.startswith("_"):
            continue  # _template.yml and friends
        slug = path.stem
        data = yaml.safe_load(path.read_text()) or {}
        entities[slug] = data
    return entities


def _build_city(
    raw: dict, defaults: dict, source_file: Path, entities: dict[str, dict] | None = None
) -> City:
    missing = [k for k in REQUIRED_CITY_KEYS if not raw.get(k)]
    missing += [k for k in PRESENT_BUT_MAY_BE_BLANK if k not in raw]
    if missing:
        raise ValueError(f"{source_file.name}: missing required keys: {', '.join(missing)}")

    _validate_slug_format(raw["slug"], source_file=source_file, kind="slug")
    for alias in raw.get("aliases") or []:
        _validate_slug_format(str(alias), source_file=source_file, kind="alias")
    source_id = _parse_source_id(raw.get("source_id"), source_file=source_file)
    uid_overrides = _parse_uid_overrides(raw.get("uid_overrides"), source_file=source_file)
    lifecycle = _parse_lifecycle(raw.get("lifecycle"), source_file=source_file)

    # Merge entity fields (city_website, meetings_url, state, colors) as base layer; explicit
    # feed-level values override them.
    entity_slug = raw.get("city")
    entity: dict = {}
    if entity_slug is not None:
        if entities is None or entity_slug not in entities:
            raise ValueError(
                f"{source_file.name}: 'city: {entity_slug}' references an unknown entity "
                f"(no config/cities/{entity_slug}.yml found)"
            )
        entity = entities[entity_slug]

    def _get(key, default=None):
        """Feed value overrides entity value, which overrides default."""
        if key in raw:
            return raw[key]
        if key in entity:
            return entity[key]
        return default

    provider = get_provider(raw["provider"])
    provider.validate(raw["source"])
    # SSRF/abuse gate: every source URL must be https on an allowed host (audit #S1). No DNS
    # here — the resolve/private-IP check runs at fetch time (citypods.http.GuardedHTTPAdapter).
    validate_city_sources(raw["provider"], raw["source"], _get("city_website"))

    # Like meetings_url/state, a verified companion normally belongs to the
    # city entity and is inherited by each of its body feeds.  A feed may still
    # override it explicitly for an exceptional body-specific calendar.
    aux_provider_name = _get("aux_provider")
    aux_source = _get("aux_source")
    if (aux_provider_name is None) != (aux_source is None):
        raise ValueError(f"{source_file.name}: aux_provider and aux_source must be set together")
    if aux_provider_name is not None:
        if not isinstance(aux_provider_name, str) or not aux_provider_name.strip():
            raise ValueError(f"{source_file.name}: aux_provider must be a non-empty provider name")
        if not isinstance(aux_source, dict):
            raise ValueError(f"{source_file.name}: aux_source must be a mapping")
        aux_provider = get_provider(aux_provider_name)
        aux_provider.validate(aux_source)
        validate_city_sources(aux_provider_name, aux_source, _get("city_website"))

    known = (
        set(REQUIRED_CITY_KEYS)
        | set(PRESENT_BUT_MAY_BE_BLANK)
        | {
            "city",
            "source_id",
            "uid_overrides",
            "lifecycle",
            "aux_provider",
            "aux_source",
            "state",
            "city_website",
            "meetings_url",
            "podcast_language",
            "podcast_category",
            "max_episodes",
            "extract_audio",
            "body_exclude",
            "colors",
            "aliases",
            "asr_enabled",
            "asr_model",
            "asr_compute_type",
            "asr_language",
            "asr_workers",
            "asr_beam_size",
            "asr_alignment_enabled",
        }
    )
    return City(
        slug=raw["slug"],
        provider=raw["provider"],
        source=raw["source"],
        podcast_title=raw["podcast_title"],
        podcast_author=raw["podcast_author"],
        podcast_email=raw["podcast_email"],
        podcast_description=raw["podcast_description"],
        source_id=source_id,
        uid_overrides=uid_overrides,
        lifecycle=lifecycle,
        aux_provider=aux_provider_name,
        aux_source=dict(aux_source) if isinstance(aux_source, dict) else None,
        city_entity=entity_slug,
        state=_get("state"),
        city_website=_get("city_website"),
        meetings_url=_get("meetings_url"),
        podcast_language=_get("podcast_language", defaults.get("podcast_language", "en-us")),
        podcast_category=_get("podcast_category", defaults.get("podcast_category", "Government")),
        max_episodes=int(_get("max_episodes", defaults.get("max_episodes", 50))),
        extract_audio=bool(_get("extract_audio", defaults.get("extract_audio", False))),
        body_exclude=list(_get("body_exclude", defaults.get("body_exclude", []))),
        colors=[str(c) for c in _get("colors", [])],
        aliases=[str(a) for a in _get("aliases", [])],
        extra={k: v for k, v in raw.items() if k not in known},
        asr_enabled=bool(_get("asr_enabled", defaults.get("asr_enabled", True))),
        asr_model=str(_get("asr_model", defaults.get("asr_model", "large-v3-turbo"))),
        asr_compute_type=str(_get("asr_compute_type", defaults.get("asr_compute_type", "int8"))),
        asr_language=str(_get("asr_language", defaults.get("asr_language", "en"))),
        asr_workers=_validate_asr_workers(
            int(_get("asr_workers", defaults.get("asr_workers", 1))), source_file=source_file
        ),
        asr_beam_size=int(_get("asr_beam_size", defaults.get("asr_beam_size", 5))),
        asr_alignment_enabled=bool(
            _get("asr_alignment_enabled", defaults.get("asr_alignment_enabled", False))
        ),
    )


# Top-level files/dirs the build owns directly; a feed slug/alias landing on one of these
# would write into (or be pruned alongside) the build's own reserved tree. Shared with
# run.py's _prune_stale_dirs, which must leave these alone.
RESERVED_PUBLIC_DIRS = {"audio", "assets", "data", "search", "static", ".git"}


def load_city_configs(config_dir: str | Path, defaults: dict) -> list[City]:
    """Load every feed from ``config/feeds/*.yml``, merging entity fields from
    ``config/cities/*.yml`` (referenced per feed via the ``city:`` key)."""
    config_dir = Path(config_dir)
    feeds_dir = config_dir / "feeds"
    entities = load_entity_configs(config_dir / "cities")
    cities: list[City] = []
    seen_slugs: set[str] = set()
    seen_source_ids: dict[str, tuple[str | None, str, dict, str]] = {}
    files: dict[str, str] = {}  # slug -> source filename, for clearer collision errors
    for path in sorted(feeds_dir.glob("*.yml")):
        if path.name.startswith("_"):
            continue  # _template.yml and friends
        raw = yaml.safe_load(path.read_text()) or {}
        city = _build_city(raw, defaults, path, entities)
        if city.slug in RESERVED_PUBLIC_DIRS:
            raise ValueError(f"{path.name}: slug {city.slug!r} collides with a reserved docs path")
        if city.slug in seen_slugs:
            raise ValueError(f"{path.name}: duplicate slug {city.slug!r}")
        seen_slugs.add(city.slug)
        if city.source_id:
            identity_source = {k: v for k, v in city.source.items() if k != "body"}
            identity = (city.city_entity, city.provider, identity_source, path.name)
            prior = seen_source_ids.get(city.source_id)
            if prior is not None and prior[:3] != identity[:3]:
                raise ValueError(
                    f"{path.name}: source_id {city.source_id!r} conflicts with {prior[3]!r}; "
                    "shared source_id values must have the same city, provider, and source "
                    "configuration apart from body"
                )
            seen_source_ids[city.source_id] = identity
        files[city.slug] = path.name
        cities.append(city)

    # Aliases become redirect dirs written *after* the real feeds, so an alias that collides
    # with a real slug (or another alias) would silently overwrite a live feed with a redirect
    # stub. Reject collisions up front.
    seen_aliases: dict[str, str] = {}
    for city in cities:
        for alias in city.aliases:
            if alias in RESERVED_PUBLIC_DIRS:
                raise ValueError(
                    f"{files[city.slug]}: alias {alias!r} collides with a reserved docs path"
                )
            if alias in seen_slugs:
                raise ValueError(
                    f"{files[city.slug]}: alias {alias!r} collides with the slug of an "
                    f"existing feed (it would overwrite that feed with a redirect)"
                )
            if alias in seen_aliases:
                raise ValueError(
                    f"{files[city.slug]}: alias {alias!r} already used by {seen_aliases[alias]!r}"
                )
            seen_aliases[alias] = city.slug
    return cities


def filter_city_configs(cities: list[City], selector: str) -> list[City]:
    """Select feeds by feed slug or city-entity slug.

    Historically CLI ``--city`` matched a single feed slug. Since feeds now reference shared
    ``config/cities/<entity>.yml`` records, the documented and more useful behavior is for an entity
    slug (for example ``austin-tx``) to select every feed for that city. Preserve exact feed-slug
    selection when present so targeted one-feed runs stay stable.
    """
    by_feed = [c for c in cities if c.slug == selector]
    if by_feed:
        return by_feed
    return [c for c in cities if c.city_entity == selector]
