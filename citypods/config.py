"""Load global site config and per-city YAML into validated models."""

from __future__ import annotations

from pathlib import Path

import yaml

from citypods.models import City
from citypods.providers import get_provider

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


def _build_city(raw: dict, defaults: dict, source_file: Path) -> City:
    missing = [k for k in REQUIRED_CITY_KEYS if not raw.get(k)]
    missing += [k for k in PRESENT_BUT_MAY_BE_BLANK if k not in raw]
    if missing:
        raise ValueError(f"{source_file.name}: missing required keys: {', '.join(missing)}")

    provider = get_provider(raw["provider"])
    provider.validate(raw["source"])

    known = (
        set(REQUIRED_CITY_KEYS)
        | set(PRESENT_BUT_MAY_BE_BLANK)
        | {
            "state",
            "city_website",
            "podcast_language",
            "podcast_category",
            "max_episodes",
            "extract_audio",
            "body_exclude",
            "colors",
            "aliases",
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
        state=raw.get("state"),
        city_website=raw.get("city_website"),
        podcast_language=raw.get("podcast_language", defaults.get("podcast_language", "en-us")),
        podcast_category=raw.get(
            "podcast_category", defaults.get("podcast_category", "Government")
        ),
        max_episodes=int(raw.get("max_episodes", defaults.get("max_episodes", 50))),
        extract_audio=bool(raw.get("extract_audio", defaults.get("extract_audio", False))),
        body_exclude=list(raw.get("body_exclude", defaults.get("body_exclude", []))),
        colors=[str(c) for c in raw.get("colors", [])],
        aliases=[str(a) for a in raw.get("aliases", [])],
        extra={k: v for k, v in raw.items() if k not in known},
    )


def load_city_configs(cities_dir: str | Path, defaults: dict) -> list[City]:
    cities_dir = Path(cities_dir)
    cities: list[City] = []
    seen_slugs: set[str] = set()
    files: dict[str, str] = {}  # slug -> source filename, for clearer collision errors
    for path in sorted(cities_dir.glob("*.yml")):
        if path.name.startswith("_"):
            continue  # _template.yml and friends
        raw = yaml.safe_load(path.read_text()) or {}
        city = _build_city(raw, defaults, path)
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
