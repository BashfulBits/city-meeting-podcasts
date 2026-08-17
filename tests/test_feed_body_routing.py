"""Regression tests pinning the body labels resolved for issue #1231.

These run against the *real* `config/` tree rather than a fixture: the point is to catch a
future selector edit that silently re-opens one of these findings, or that widens one feed's
selector until it starts claiming a sibling feed's meetings. Every city here publishes several
feeds from a single provider source, so a selector is only correct if it both matches its own
label and leaves its siblings' labels alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from citypods.bodies import matches, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="module")
def feeds() -> dict[str, object]:
    return {city.slug: city for city in load_city_configs(CONFIG_DIR, {})}


# (provider label, owning feed, sibling feeds that must NOT claim it).  Labels are verbatim from
# the audit finding in #1231 -- including Fort Worth's provider-duplicated label, which is why a
# single un-duplicated selector has to keep matching it by substring.
ROUTED_LABELS = [
    pytest.param(
        "Special Meeting",
        "addison-tx-city-council",
        [
            "addison-tx-board-of-zoning-adjustment",
            "addison-tx-planning-and-zoning-commission",
            "addison-tx-comprehensive-plan-advisory-committee",
            "addison-tx-town-meetings",
        ],
        id="addison-bare-special-meeting",
    ),
    pytest.param(
        "Audit and Finance Committee Audit and Finance Committee",
        "fort-worth-tx-audit-committee",
        [],
        id="fort-worth-duplicated-label",
    ),
    pytest.param(
        "TIRZ",
        "pflugerville-tx-tirz-board",
        [
            "pflugerville-tx-city-council",
            "pflugerville-tx-library-board",
            "pflugerville-tx-planning-and-zoning-commission",
            "pflugerville-tx-capital-improvement-advisory-committee",
        ],
        id="pflugerville-bare-tirz",
    ),
]


@pytest.mark.parametrize(("label", "owner", "siblings"), ROUTED_LABELS)
def test_recurring_label_routes_to_exactly_one_feed(label, owner, siblings, feeds) -> None:
    assert matches(label, source_body_filter(feeds[owner].source))
    claimed = [s for s in siblings if matches(label, source_body_filter(feeds[s].source))]
    assert claimed == [], f"{label!r} also captured by {claimed}"


# (provider GUID, the one feed allowed to pin it).  A one-off inclusion is only correct if
# exactly one feed carries it -- two would publish the same recording twice.
PINNED_GUIDS = [
    (
        "https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3622",
        "arlington-tx-council",
    ),
    ("205110", "dallas-tx-bid-purchasing"),
    ("13509", "denton-tx-city-council"),
    # An advisory board, not a Council session: it belongs with boards/commissions even though
    # both feeds read the same Swagit view.
    ("392481", "waco-tx-boards-and-commissions-committee"),
]


@pytest.mark.parametrize(("provider_guid", "owner"), PINNED_GUIDS)
def test_one_off_guid_is_pinned_to_exactly_one_feed(provider_guid, owner, feeds) -> None:
    holders = [
        slug
        for slug, city in feeds.items()
        if any(inc.provider_guid == provider_guid for inc in source_body_inclusions(city.source))
    ]
    assert holders == [owner]


def test_tirz_selector_needs_no_body_any_alternative(feeds) -> None:
    """`body: "TIRZ"` subsumes every longer TIRZ label, so a `body_any` entry would be dead."""
    selector = source_body_filter(feeds["pflugerville-tx-tirz-board"].source)
    assert selector == "TIRZ"
    assert matches("TIRZ Board", selector)
    assert matches("TIRZ Board Meeting", selector)
