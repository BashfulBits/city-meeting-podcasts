"""A verified task-force proceeding retains recording identity in its own feed."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_actual_config_keeps_cbtf_only_in_its_task_force_feed():
    """Actual selectors retain original RSS identity and reject Council leakage."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "dallas-tx-city-council")
    commission = next(c for c in cities if c.slug == "dallas-tx-2024-community-bond-task-force")
    record = json.loads((root / "tests/fixtures/dallas-cbtf-retained.json").read_text())
    original = copy.deepcopy(record)
    assert not record_matches_body(
        record, source_body_filter(council.source), source_body_inclusions(council.source)
    )
    assert record_matches_body(
        record, source_body_filter(commission.source), source_body_inclusions(commission.source)
    )
    rss = build_rss(
        commission, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
    )
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    negative = dict(record, provider_guid="unrelated")
    assert not record_matches_body(
        negative, source_body_filter(commission.source), source_body_inclusions(commission.source)
    )
    assert record == original
