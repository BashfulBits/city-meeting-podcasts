"""Load global site config and per-city YAML into validated models."""

from __future__ import annotations

from pathlib import Path

import yaml

from citypods.models import City
from citypods.providers import get_provider
from citypods.security import validate_city_sources

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


def load_site_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    data.setdefault("defaults", {})
    return data


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

    known = (
        set(REQUIRED_CITY_KEYS)
        | set(PRESENT_BUT_MAY_BE_BLANK)
        | {
            "city",
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
        asr_model=str(_get("asr_model", defaults.get("asr_model", "distil-large-v3"))),
        asr_compute_type=str(_get("asr_compute_type", defaults.get("asr_compute_type", "int8"))),
        asr_language=str(_get("asr_language", defaults.get("asr_language", "en"))),
        asr_workers=int(_get("asr_workers", defaults.get("asr_workers", 1))),
        asr_beam_size=int(_get("asr_beam_size", defaults.get("asr_beam_size", 5))),
    )


def load_city_configs(config_dir: str | Path, defaults: dict) -> list[City]:
    """Load every feed from ``config/feeds/*.yml``, merging entity fields from
    ``config/cities/*.yml`` (referenced per feed via the ``city:`` key)."""
    config_dir = Path(config_dir)
    feeds_dir = config_dir / "feeds"
    entities = load_entity_configs(config_dir / "cities")
    cities: list[City] = []
    seen_slugs: set[str] = set()
    files: dict[str, str] = {}  # slug -> source filename, for clearer collision errors
    for path in sorted(feeds_dir.glob("*.yml")):
        if path.name.startswith("_"):
            continue  # _template.yml and friends
        raw = yaml.safe_load(path.read_text()) or {}
        city = _build_city(raw, defaults, path, entities)
        if city.slug in seen_slugs:
            raise ValueError(f"{path.name}: duplicate slug {city.slug!r}")
        seen_slugs.add(city.slug)
        files[city.slug] = path.name
        cities.append(city)

    # Aliases become redirect dirs written *after* the real feeds, so an alias that collides
    # with a real slug (or another alias) would silently overwrite a live feed with a redirect
    # stub. Reject collisions up front.
    seen_aliases: dict[str, str] = {}
    for city in cities:
        for alias in city.aliases:
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
