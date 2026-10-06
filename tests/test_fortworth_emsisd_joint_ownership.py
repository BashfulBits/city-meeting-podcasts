"""Verified Council/EMSISD joint keeps each participant and original whole audio."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_proven_emsisd_joint_preserves_council_audio_and_raw_identity():
    root = Path(__file__).resolve().parents[1]
    configs = {c.slug: c for c in load_city_configs(root / "config", {})}
    board = configs["fort-worth-tx-emsisd-board-joint-meetings"]
    council = configs["fort-worth-tx-city-council"]
    rows = json.loads((root / "tests/fixtures/fortworth-emsisd-joint-retained.json").read_text())
    original = copy.deepcopy(rows)
    row = rows[0]

    def owns(config, record):
        return record_matches_body(
            record, source_body_filter(config.source), source_body_inclusions(config.source)
        )

    assert owns(board, row)
    assert owns(council, row)
    assert owns(board, dict(row, provider_guid="future-same-proven-participants"))
    for label in ("City Council", "Other School Board and Fort Worth City Council", "EMSISD Board"):
        assert not owns(board, dict(row, body=label, provider_guid="unproved-participants"))
    for config in (board, council):
        rss = build_rss(config, [record_to_episode(row)], "audio", "https://www.citymeetings.fyi")
        assert row["uid"] in rss
        assert row["audio"]["url"] in rss
    assert board.source["feed_urls"] == council.source["feed_urls"]
    assert rows == original
