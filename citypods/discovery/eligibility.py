"""Weekly auxiliary-discovery eligibility and native-agenda coverage measurement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from citypods.models import City

# A city with one of these primary providers already has an official agenda-capable adapter.
NATIVE_AGENDA_PROVIDERS = frozenset({"civicclerk", "civicengage", "legistar", "onemeeting"})


@dataclass(frozen=True)
class AgendaCoverage:
    numerator: int
    denominator: int
    measured_at: str

    @property
    def ratio(self) -> float:
        return self.numerator / self.denominator if self.denominator else 0.0


def measure_agenda_coverage(
    records: dict[str, dict[str, Any]], *, now: datetime | None = None
) -> AgendaCoverage:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=365)
    expected = verified = 0
    for record in records.values():
        raw = record.get("published")
        if not isinstance(raw, str):
            continue
        try:
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        if published < cutoff:
            continue
        expected += 1
        links = record.get("links")
        if isinstance(links, dict) and any(
            isinstance(links.get(key), str) and links[key]
            for key in ("agenda", "agenda_portal", "agenda_packet")
        ):
            verified += 1
    return AgendaCoverage(verified, expected, now.isoformat())


def auxiliary_eligibility(
    city: City, coverage: AgendaCoverage, *, prior_state: str | None = None, low_checks: int = 0
) -> str:
    """Return an R12 state without turning a tiny sample into a coverage claim."""
    if city.aux_provider:
        return "verified"
    if city.provider in NATIVE_AGENDA_PROVIDERS:
        return "agenda-covered"
    if coverage.denominator >= 5 and coverage.ratio >= 0.95:
        return "agenda-covered"
    if prior_state == "agenda-covered" and low_checks < 2:
        return "agenda-covered"
    return "eligible"


def auxiliary_states(
    cities: list[City],
    records_for_city: dict[str, dict[str, dict[str, Any]]],
    prior: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Measure and persist coverage state without hardcoding an auxiliary-city list."""
    eligible: list[str] = []
    state: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[City]] = {}
    for city in cities:
        grouped.setdefault(city.city_entity or city.slug, []).append(city)
    for entity, entity_cities in grouped.items():
        city = entity_cities[0]
        records = {
            f"{feed.slug}:{key}": value
            for feed in entity_cities
            for key, value in records_for_city.get(feed.slug, {}).items()
        }
        coverage = measure_agenda_coverage(records)
        previous = prior.get(entity) if isinstance(prior.get(entity), dict) else {}
        prior_state = previous.get("status")
        previous_low = int(previous.get("low_coverage_checks", 0) or 0)
        low_checks = (
            previous_low + 1
            if prior_state == "agenda-covered"
            and coverage.denominator >= 5
            and coverage.ratio < 0.90
            else 0
        )
        status = auxiliary_eligibility(
            city, coverage, prior_state=prior_state, low_checks=low_checks
        )
        state[entity] = {
            "status": status,
            "low_coverage_checks": low_checks,
            "agenda_coverage": {
                "numerator": coverage.numerator,
                "denominator": coverage.denominator,
                "ratio": coverage.ratio,
                "measured_at": coverage.measured_at,
                "evidence_source": "persisted meeting records",
            },
        }
        if status == "eligible":
            eligible.extend(feed.slug for feed in entity_cities)
    return sorted(eligible), state
