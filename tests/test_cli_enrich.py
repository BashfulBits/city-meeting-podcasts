"""CLI wiring for the H6b sharded enrich lanes: shard parsing + arg plumbing into build()."""

from __future__ import annotations

import pytest

from citypods import cli


def test_parse_shard_valid():
    assert cli._parse_shard("0/4") == (0, 4)
    assert cli._parse_shard("3/4") == (3, 4)
    assert cli._parse_shard(None) is None
    assert cli._parse_shard("") is None


@pytest.mark.parametrize("bad", ["4/4", "5/4", "-1/4", "2", "a/4", "1/0"])
def test_parse_shard_invalid(bad):
    with pytest.raises(SystemExit):
        cli._parse_shard(bad)


def test_enrich_threads_shard_source_lane_into_build(monkeypatch):
    """``citypods enrich --shard --source --lane`` must reach build() with the parsed values."""
    captured = {}
    monkeypatch.setattr(cli, "build", lambda **kw: captured.update(kw) or [])
    rc = cli.main(
        ["enrich", "--shard", "1/4", "--source", "abc123", "--lane", "transcribe", "--dry-run"]
    )
    assert rc == 0
    assert captured["phase"] == "enrich"
    assert captured["shard"] == (1, 4)
    assert captured["source"] == "abc123"
    assert captured["lane"] == "transcribe"
    assert captured["dry_run"] is True


def test_enrich_defaults_are_unsharded_full_lane(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "build", lambda **kw: captured.update(kw) or [])
    cli.main(["enrich"])
    assert captured["shard"] is None
    assert captured["source"] is None
    assert captured["lane"] is None
    assert captured["dry_run"] is False
