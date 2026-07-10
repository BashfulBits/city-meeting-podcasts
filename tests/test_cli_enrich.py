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
    rc = cli.main(["enrich"])
    assert rc == 0
    assert captured["shard"] is None
    assert captured["source"] is None
    assert captured["lane"] is None
    assert captured["dry_run"] is False


def test_compute_run_internal_worker_routes_to_dedicated_entrypoint(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        cli,
        "_compute_run_internal_worker",
        lambda args: captured.update(
            {
                "owner": args.owner,
                "max_claims": args.max_claims,
                "max_scan": args.max_scan,
            }
        )
        or 0,
    )

    rc = cli.main(
        [
            "compute",
            "run-internal-worker",
            "--owner",
            "github-actions:test:1",
            "--max-claims",
            "3",
            "--max-scan",
            "9",
        ]
    )

    assert rc == 0
    assert captured == {
        "owner": "github-actions:test:1",
        "max_claims": 3,
        "max_scan": 9,
    }
