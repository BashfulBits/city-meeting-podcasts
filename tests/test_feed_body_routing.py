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
# exactly one feed carries it; verified joints are separately pinned to their participants.
PINNED_GUIDS = [
    (
        "https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=3622",
        "arlington-tx-council",
    ),
    ("205110", "dallas-tx-bid-purchasing"),
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


def test_verified_library_joint_has_exact_participant_feeds(feeds) -> None:
    """The official Council/Library joint belongs to both proven participants."""
    library = feeds["denton-tx-library-board"]
    council = feeds["denton-tx-city-council"]
    joint = "Joint Luncheon with Library Board"
    assert matches(joint, source_body_filter(library.source))
    assert matches(joint, source_body_filter(council.source))
    assert not any(inc.provider_guid == "13509" for inc in source_body_inclusions(library.source))
    assert not any(inc.provider_guid == "13509" for inc in source_body_inclusions(council.source))


def test_fort_worth_development_corporation_joint_reaches_both_named_feeds(feeds) -> None:
    joint = (
        "AllianceAirport Authority, Inc., Central City Local Development Corporation, and Fort "
        "Worth Local Development Corporation, and AllianceAirport Authority, Inc., Central City "
        "Local Development Corporation, and Fort Worth Local Development Corporation, and"
    )
    alliance = source_body_filter(feeds["fort-worth-tx-alliance-airport-authority"].source)
    development = source_body_filter(
        feeds["fort-worth-tx-fort-worth-local-development-corporation"].source
    )
    central_city = source_body_filter(
        feeds["fort-worth-tx-central-city-local-government-corporation"].source
    )
    assert matches(joint, alliance)
    assert matches(joint, development)
    assert matches(joint, central_city)


def test_denton_bond_oversight_rule_matches_the_named_body_only(feeds) -> None:
    """The Denton source's full committee name catches its dated title variations."""
    feed = feeds["denton-tx-bond-oversight-committee"]
    selector = source_body_filter(feed.source)

    assert feed.source["list_url"] == "https://dentontx.new.swagit.com/views/5"
    expected_titles = (
        "Bond Oversight Committee on 2020-07-23 11:30 AM",
        "Bond Oversight Committee on 2020-11-05 12:00 PM (SPECIAL CALLED MEETING)",
        "Bond Oversight Committee on 2021-02-04 12:00 PM (AMENDED)",
        "Bond Oversight Committee on 2021-05-06 12:00 PM",
        "Bond Oversight Committee on 2027-05-06 12:00 PM",
    )
    for title in expected_titles:
        assert matches(title, selector)

    for title in expected_titles[:-1]:
        holders = [
            slug
            for slug, city in feeds.items()
            if city.provider == feed.provider
            and city.source.get("list_url") == feed.source["list_url"]
            and matches(title, source_body_filter(city.source))
        ]
        assert holders == ["denton-tx-bond-oversight-committee"]

    for title in (
        "Special Citizens Bond Advisory Committee on 2023-06-06 6:00 PM",
        "2023 Bond Program Neighborhood 4 Update",
        "City Council Bond Program Discussion",
    ):
        assert not matches(title, selector)


@pytest.mark.parametrize(
    ("slug", "historical_title", "future_title"),
    [
        (
            "fort-worth-tx-housing-and-economic-development-committee",
            "HOUSING AND ECONOMIC DEVELOPMENT COMMITTEE HOUSING AND ECONOMIC DEVELOPMENT COMMITTEE",
            "HOUSING AND ECONOMIC DEVELOPMENT COMMITTEE HOUSING AND ECONOMIC DEVELOPMENT COMMITTEE",
        ),
        (
            "fort-worth-tx-neighborhood-quality-and-revitalization-committee",
            "Neighborhood Quality And Revitalization of August 9, 2022 "
            "Neighborhood Quality And Revitalization of August 9, 2022",
            "Neighborhood Quality and Revitalization Committee "
            "Neighborhood Quality and Revitalization Committee",
        ),
        (
            "fort-worth-tx-entrepreneurship-and-innovation-committee",
            "Entrepreneurship and Innovation Committee Entrepreneurship and Innovation Committee",
            "Entrepreneurship and Innovation Committee Entrepreneurship and Innovation Committee",
        ),
        (
            "dallas-tx-youth-commission",
            "Youth Commission",
            "Youth Commission on 2027-05-06 4:00 PM",
        ),
        (
            "dallas-tx-mobility-solutions-infrastructure-and-sustainability-committee",
            "Mobility Solutions, Infrastructure and Sustainability Committee",
            "Mobility Solutions, Infrastructure and Sustainability Committee - May 6, 2027",
        ),
        (
            "dallas-tx-human-social-needs-committee",
            "Human & Social Needs Committee",
            "Human and Social Needs Committee - May 6, 2027",
        ),
        (
            "denton-tx-committee-on-the-environment",
            "Committee on the Environment on 2020-10-05 9:00 AM",
            "Committee on the Environment on 2027-05-06 9:00 AM",
        ),
        (
            "denton-tx-development-code-review-committee",
            "Development Code Review Committee on 2018-10-26 11:00 AM",
            "Development Code Review Committee on 2027-05-06 11:00 AM",
        ),
        (
            "denton-tx-public-art-committee",
            "Public Art Committee on 2020-09-04 3:00 PM",
            "Public Art Committee on 2027-05-06 3:00 PM",
        ),
        (
            "denton-tx-airport-advisory-board",
            "Airport Advisory Board on 2021-03-11 1:00 PM",
            "Airport Advisory Board on 2027-05-06 1:00 PM",
        ),
        (
            "denton-tx-library-board",
            "Library Board on 2021-03-11 3:30 PM",
            "Library Board on 2027-05-06 3:30 PM",
        ),
        (
            "denton-tx-special-citizens-bond-advisory-committee",
            "Special Citizens Bond Advisory Committee on 2023-06-26 6:00 PM",
            "Special Citizens Bond Advisory Committee on 2027-05-06 6:00 PM",
        ),
    ],
)
def test_approved_named_body_feeds_match_historical_and_future_titles(
    feeds, slug, historical_title, future_title
) -> None:
    selector = source_body_filter(feeds[slug].source)
    assert matches(historical_title, selector)
    assert matches(future_title, selector)


def test_trinity_metro_board_feed_uses_only_observed_complete_labels(feeds) -> None:
    selector = source_body_filter(feeds["fort-worth-tx-trinity-metro-board"].source)
    assert matches("T Board Meeting", selector)
    assert matches("T Board of Directors", selector)
    assert not matches("City Council T Board Meeting", selector)
    assert not matches("Transit board discussion", selector)


def test_fort_worth_transportation_ciac_uses_confirmed_labels_not_generic_substrings(feeds) -> None:
    selector = source_body_filter(
        feeds["fort-worth-tx-capital-improvements-advisory-committee"].source
    )
    assert matches("Capital Improvements Advisory Committee", selector)
    assert matches(
        "Capital Improvements Advisory Committee for Transportation Impact Fees",
        selector,
    )
    assert matches("CIAC of April 27, 2022 CIAC of April 27, 2022", selector)
    assert not matches("Capital Improvements Plan Advisory Committee - Water/Wastewater", selector)
    assert not matches("Water and Wastewater CIP Citizens Advisory Committee", selector)


def test_fort_worth_ciac_joint_with_city_plan_commission_reaches_both_feeds(feeds) -> None:
    joint = (
        "City Plan Commission and Capital Improvements Advisory Committee (CIAC) "
        "City Plan Commission and Capital Improvements Advisory Committee (CIAC)"
    )
    ciac = source_body_filter(feeds["fort-worth-tx-capital-improvements-advisory-committee"].source)
    planning = source_body_filter(feeds["fort-worth-tx-city-plan-commission"].source)
    assert matches(joint, ciac)
    assert matches(joint, planning)


def test_dallas_pensions_joint_with_finance_reaches_both_feeds(feeds) -> None:
    joint = "Joint Meeting of the Ad Hoc Committee on Pensions and the Finance Committee"
    pensions = source_body_filter(feeds["dallas-tx-ad-hoc-committee-on-pensions"].source)
    finance = source_body_filter(feeds["dallas-tx-finance-committee"].source)
    assert matches(joint, pensions)
    assert matches(joint, finance)


def test_fort_worth_named_rules_follow_the_full_name_without_status_exceptions(feeds) -> None:
    housing = source_body_filter(
        feeds["fort-worth-tx-housing-and-economic-development-committee"].source
    )
    neighborhood = source_body_filter(
        feeds["fort-worth-tx-neighborhood-quality-and-revitalization-committee"].source
    )
    entrepreneurship = source_body_filter(
        feeds["fort-worth-tx-entrepreneurship-and-innovation-committee"].source
    )
    assert matches(
        "Housing and Economic Development Committee #1 "
        "Housing and Economic Development Committee #1",
        housing,
    )
    assert matches(
        "Housing and Economic Development Committee #2 "
        "Housing and Economic Development Committee #2",
        housing,
    )
    joint_label = (
        "Mobility: Infrastructure and Transportation Committee & "
        "Neighborhood Quality and Revitalization Committee "
        "Mobility: Infrastructure and Transportation Committee & "
        "Neighborhood Quality and Revitalization Committee"
    )
    infrastructure = source_body_filter(
        feeds["fort-worth-tx-infrastructure-and-growth-committee"].source
    )
    assert matches(joint_label, neighborhood)
    assert matches(joint_label, infrastructure)
    assert matches("Entrepreneurship and Innovation Committee - CANCELED", entrepreneurship)


def test_fort_worth_joint_titles_reach_both_named_participant_feeds(feeds) -> None:
    joint = (
        "Human Relations Commission - Mayor's Committee on Persons with Disabilities "
        "Human Relations Commission - Mayor's Committee on Persons with Disabilities"
    )
    human_relations = source_body_filter(feeds["fort-worth-tx-human-relations-commission"].source)
    disabilities = source_body_filter(
        feeds["fort-worth-tx-mayor-s-committee-on-persons-with-disabilities"].source
    )
    assert matches(joint, human_relations)
    assert matches(joint, disabilities)


def test_denton_named_bond_feeds_keep_oversight_and_cycle_bodies_separate(feeds) -> None:
    oversight = source_body_filter(feeds["denton-tx-bond-oversight-committee"].source)
    cycle = source_body_filter(feeds["denton-tx-special-citizens-bond-advisory-committee"].source)
    assert matches("Bond Oversight Committee on 2027-05-06 12:00 PM", oversight)
    assert not matches("Bond Oversight Committee on 2027-05-06 12:00 PM", cycle)
    assert matches("Special Citizens Bond Advisory Committee on 2027-05-06 6:00 PM", cycle)
    assert not matches("Special Citizens Bond Advisory Committee on 2027-05-06 6:00 PM", oversight)


def test_denton_name_rules_do_not_add_status_specific_exceptions(feeds) -> None:
    development = source_body_filter(feeds["denton-tx-development-code-review-committee"].source)
    library = source_body_filter(feeds["denton-tx-library-board"].source)
    assert matches(
        "Development Code Review Committee on 2027-05-06 11:00 AM",
        development,
    )
    assert matches(
        "Development Code Review Committee on 2018-06-08 11:00 AM - Cancelled",
        development,
    )
    assert matches("Library Board on 2027-05-06 3:30 PM", library)
    assert matches(
        "Library Board on 2018-08-13 5:30 PM (No quorum; meeting not held)",
        library,
    )


def test_fort_worth_four_corporation_joint_title_reaches_each_named_body(feeds) -> None:
    joint = "LDC, CCLGC, RILGC, FWHFC"
    slugs = (
        "fort-worth-tx-fort-worth-local-development-corporation",
        "fort-worth-tx-central-city-local-government-corporation",
        "fort-worth-tx-research-and-innovation-local-government-corporation",
        "fort-worth-tx-fort-worth-housing-finance-corporation",
    )
    assert all(matches(joint, source_body_filter(feeds[slug].source)) for slug in slugs)
