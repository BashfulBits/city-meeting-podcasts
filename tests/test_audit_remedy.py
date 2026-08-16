"""Unit tests for citypods.audit_remedy."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml

from citypods.audit_remedy import (
    BodyClassification,
    RemedyOutput,
    YAMLMutation,
    apply_remedy_mutations,
    classify_unexpected_bodies,
    format_remedy_markdown_table,
    gather_unexpected_body_evidence,
)
from citypods.compute.base import JobResult
from citypods.models import City, Episode
from tests._cas_fake import MemStorage


def test_gather_unexpected_body_evidence(tmp_path: Path) -> None:
    config_cities = tmp_path / "config" / "cities"
    config_cities.mkdir(parents=True)
    city_yaml = config_cities / "test-city-tx.yml"
    city_yaml.write_text(
        yaml.safe_dump(
            {
                "name": "Test City",
                "state": "TX",
                "city_website": "https://testcity.gov",
                "meetings_url": "https://testcity.gov/meetings",
            }
        ),
        encoding="utf-8",
    )

    feed_city = City(
        slug="test-city-tx-council",
        city_entity="test-city-tx",
        provider="granicus",
        source={"feed_url": "https://test.example/feed", "body": "City Council"},
        podcast_title="Test City: City Council",
        podcast_description="Test description",
        podcast_author="Test Author",
        podcast_email="test@example.com",
    )

    episodes = [
        Episode(
            guid="guid-101",
            title="Special Meeting - Budget Workshop",
            published=datetime.fromisoformat("2026-05-10T18:00:00+00:00"),
            body="Special Meeting",
            video_url="https://test.example/video1",
        ),
        Episode(
            guid="guid-102",
            title="Special Meeting - Public Hearing",
            published=datetime.fromisoformat("2026-06-15T18:00:00+00:00"),
            body="Special Meeting",
            video_url="https://test.example/video2",
        ),
    ]

    records = {
        "guid-1": {"body": "City Council", "title": "Council Regular Meeting"},
    }

    evidence = gather_unexpected_body_evidence(
        source_key="test-city-granicus",
        city_slug="test-city-tx",
        unmatched_episodes=episodes,
        related_cities=[feed_city],
        records=records,
        repo_root=tmp_path,
    )

    assert evidence["source_key"] == "test-city-granicus"
    assert evidence["city"]["name"] == "Test City"
    assert len(evidence["existing_feeds"]) == 1
    assert evidence["existing_feeds"][0]["slug"] == "test-city-tx-council"
    assert len(evidence["unexpected_findings"]) == 1
    finding = evidence["unexpected_findings"][0]
    assert finding["unexpected_body"] == "Special Meeting"
    assert finding["count"] == 2
    assert finding["date_range"]["earliest"] == "2026-05-10T18:00:00+00:00"
    assert finding["date_range"]["latest"] == "2026-06-15T18:00:00+00:00"


def test_apply_remedy_mutations(tmp_path: Path) -> None:
    feed_path = tmp_path / "config" / "feeds" / "test-city-council.yml"
    feed_path.parent.mkdir(parents=True)
    feed_path.write_text(
        yaml.safe_dump(
            {
                "slug": "test-city-council",
                "city": "test-city-tx",
                "provider": "granicus",
                "source": {
                    "body": "City Council",
                    "body_any": ["Work Session"],
                },
            }
        ),
        encoding="utf-8",
    )

    remedy = RemedyOutput(
        classifications=[
            BodyClassification(
                source_key="test-city-granicus",
                unexpected_body="Special Meeting",
                action="union",
                target_feeds=["test-city-council"],
                rationale="Recurring council session",
                mutations=[
                    YAMLMutation(
                        file_path="config/feeds/test-city-council.yml",
                        action="add_body_any",
                        content={"body_any": ["Special Meeting"]},
                    )
                ],
            ),
            BodyClassification(
                source_key="test-city-granicus",
                unexpected_body="Joint Luncheon",
                action="single_uid_inclusion",
                target_feeds=["test-city-council"],
                rationale="One-off event",
                mutations=[
                    YAMLMutation(
                        file_path="config/feeds/test-city-council.yml",
                        action="add_body_includes",
                        content={
                            "body_includes": [{"provider_guid": "12345", "body": "Joint Luncheon"}]
                        },
                    )
                ],
            ),
            BodyClassification(
                source_key="test-city-granicus",
                unexpected_body="TIRZ Board",
                action="new_feed",
                target_feeds=["test-city-tirz-board"],
                rationale="Recurring board series",
                mutations=[
                    YAMLMutation(
                        file_path="config/feeds/test-city-tirz-board.yml",
                        action="create_feed",
                        content={
                            "slug": "test-city-tirz-board",
                            "city": "test-city-tx",
                            "provider": "granicus",
                            "source": {"body": "TIRZ Board"},
                        },
                    )
                ],
            ),
        ]
    )

    modified = apply_remedy_mutations(remedy, repo_root=tmp_path)
    assert len(modified) == 2

    # Verify council feed mutations
    council_data = yaml.safe_load(feed_path.read_text(encoding="utf-8"))
    assert "Special Meeting" in council_data["source"]["body_any"]
    assert "Work Session" in council_data["source"]["body_any"]
    assert any(x["provider_guid"] == "12345" for x in council_data["source"]["body_includes"])

    # Verify new feed creation
    tirz_path = tmp_path / "config" / "feeds" / "test-city-tirz-board.yml"
    assert tirz_path.exists()
    tirz_data = yaml.safe_load(tirz_path.read_text(encoding="utf-8"))
    assert tirz_data["slug"] == "test-city-tirz-board"


def test_classify_unexpected_bodies_with_mock_backend() -> None:
    remedy_payload = {
        "classifications": [
            {
                "source_key": "test-source",
                "unexpected_body": "Special Meeting",
                "action": "union",
                "target_feeds": ["test-council"],
                "rationale": "Recurring special session",
                "mutations": [
                    {
                        "file_path": "config/feeds/test-council.yml",
                        "action": "add_body_any",
                        "content": {"body_any": ["Special Meeting"]},
                    }
                ],
            }
        ]
    }

    class FakeBackend:
        def run_inference(self, job):
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": json.dumps(remedy_payload)}}]},
                model="gemini/gemini-3.7-flash",
            )

    output = classify_unexpected_bodies(
        evidence_bundle=[{"source_key": "test-source"}],
        storage=MemStorage(),
        backend=FakeBackend(),  # type: ignore
    )
    assert len(output.classifications) == 1
    assert output.classifications[0].action == "union"
    assert output.classifications[0].target_feeds == ["test-council"]


def test_format_remedy_markdown_table() -> None:
    remedy = RemedyOutput(
        classifications=[
            BodyClassification(
                source_key="addison-granicus",
                unexpected_body="Special Meeting",
                action="union",
                target_feeds=["addison-tx-city-council"],
                rationale="35 recurring special meetings",
                mutations=[],
            ),
            BodyClassification(
                source_key="denton-swagit",
                unexpected_body="Joint Luncheon with Library Board",
                action="single_uid_inclusion",
                target_feeds=["denton-tx-city-council", "denton-tx-library-board"],
                rationale="One-off joint session between Council and Library Board",
                mutations=[],
            ),
        ]
    )

    table = format_remedy_markdown_table(remedy)
    assert "| Source | Unexpected Body | Action | Target Feed(s) | Rationale |" in table
    assert "`addison-granicus`" in table
    assert "**union**" in table
    assert "`denton-tx-city-council`, `denton-tx-library-board`" in table
