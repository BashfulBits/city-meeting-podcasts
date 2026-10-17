"""Official agendas distinguish formal taskforce from identically labeled public town halls."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_2017_exact_notices_route_distinct_proceedings_and_preserve_audio():
    root = Path(__file__).resolve().parents[1]
    configs = {c.slug: c for c in load_city_configs(root / "config", {})}
    formal = configs["dallas-tx-2017-citizens-bond-task-force"]
    public = configs["dallas-tx-2017-bond-public-town-halls"]
    rows = json.loads((root / "tests/fixtures/dallas-2017-bond-retained.json").read_text())
    original = copy.deepcopy(rows)

    def owns(config, row):
        return record_matches_body(
            row, source_body_filter(config.source), source_body_inclusions(config.source)
        )

    for row in rows:
        is_formal = row["provider_guid"] == "202453"
        assert owns(formal, row) is is_formal
        assert owns(public, row) is not is_formal
        unknown = dict(row, provider_guid="unbound-shared-label")
        assert not owns(formal, unknown)
        assert not owns(public, unknown)
        owner = formal if is_formal else public
        rss = build_rss(owner, [record_to_episode(row)], "audio", "https://www.citymeetings.fyi")
        assert row["uid"] in rss
        assert row["audio"]["url"] in rss
    assert rows == original
