from __future__ import annotations

from types import SimpleNamespace

from citypods.compute.base import InferenceJob, JobResult
from citypods.discovery.classify import classify, parse_classification
from citypods.discovery.config import discovery_llm_config
from citypods.discovery.eligibility import AgendaCoverage, auxiliary_eligibility
from citypods.discovery.models import Classification, DiscoveryRequest, SearchResult
from citypods.discovery.render import parse_evidence_marker, parse_state_marker, render_evidence
from citypods.discovery.verify import verify_discovery
from scripts import city_discovery as city_discovery_script


def _request(mode="new-city"):
    return DiscoveryRequest(
        mode=mode,
        city_name="Example",
        state="TX",
        city_slug="example-tx",
        known_provider="swagit" if mode == "auxiliary" else None,
        city_website="https://example.gov",
        meeting_url_hint="https://example.gov/meetings",
    )


def _results():
    return [
        SearchResult("https://example.granicus.com/ViewPublisherRSS.php?view_id=1", "Meetings"),
        SearchResult("https://example.gov", "City of Example"),
    ]


def test_discovery_llm_route_is_task_scoped_yaml_not_generic_environment(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.setenv("LLM_MODE", "dispatch")

    config = discovery_llm_config(
        {
            "city_discovery": {
                "llm_model": "gemini/gemini-3-flash-preview",
                "llm_mode": "direct",
            }
        }
    )

    assert config.model == "gemini/gemini-3-flash-preview"
    assert config.mode == "direct"


def test_auxiliary_eligibility_keeps_state_restore_logs_off_json_stdout(
    monkeypatch, capsys, tmp_path
):
    def fake_pull(_site, _output_dir, *, log):
        log("state: restored 3 file(s) from durable storage")
        return tmp_path

    monkeypatch.setattr(city_discovery_script, "pull_canonical_state", fake_pull)
    monkeypatch.setattr(city_discovery_script, "load_site_config", lambda *_: {"defaults": {}})
    monkeypatch.setattr(city_discovery_script, "load_city_configs", lambda *_: [])
    monkeypatch.setattr(city_discovery_script, "auxiliary_states", lambda *_: ([], {}))

    result = city_discovery_script._eligible_auxiliary(
        SimpleNamespace(
            site_config="config/site_config.yml",
            output_dir=tmp_path,
            pull_state=True,
            config_dir="config",
            prior_aux_state=None,
        )
    )

    captured = capsys.readouterr()
    assert result == {"eligible": [], "state": {}}
    assert captured.out == ""
    assert "state: restored 3 file(s) from durable storage" in captured.err


def test_classifier_rejects_source_url_not_in_retrieved_evidence():
    response = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"video_platform":"granicus","agenda_platform":null,'
                        '"city_identity":"confirmed",'
                        '"candidate_urls":["https://example.granicus.com/ViewPublisherRSS.php?view_id=1"],'
                        '"video_source":{"feed_url":"https://evil.example/feed"},'
                        '"bodies_mentioned":[],"confidence":"high","reasoning":"test"}'
                    )
                }
            }
        ]
    }
    parsed = parse_classification(response, _request(), _results())
    assert parsed.video_source is None


def test_classifier_prompt_includes_provider_source_schemas():
    class Backend:
        def run_inference(self, job: InferenceJob):
            prompt = job.inputs["messages"][0]["content"]
            assert "minutes_url" in prompt
            assert "granicus_base" in prompt
            assert "city_identity" in prompt
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"video_platform":"granicus","agenda_platform":null,'
                                    '"city_identity":"confirmed",'
                                    '"candidate_urls":["https://example.granicus.com/ViewPublisherRSS.php?view_id=1"],'
                                    '"video_source":{"feed_url":"https://example.granicus.com/ViewPublisherRSS.php?view_id=1"},'
                                    '"bodies_mentioned":[],"confidence":"high","reasoning":"test"}'
                                )
                            }
                        }
                    ]
                },
            )

    result = classify(Backend(), _request(), _results())
    assert result.video_source == {
        "feed_url": "https://example.granicus.com/ViewPublisherRSS.php?view_id=1"
    }


def test_classifier_discards_foreign_city_provider_details():
    response = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"city_identity":"mismatch","video_platform":"civicengage",'
                        '"agenda_platform":"civicengage",'
                        '"candidate_urls":["https://example.gov/foreign"],'
                        '"video_source":{"feed_url":"https://example.gov/foreign"},'
                        '"bodies_mentioned":["Foreign City Council"],'
                        '"confidence":"medium","reasoning":"Evidence is for another city."}'
                    )
                }
            }
        ]
    }
    result = parse_classification(
        response,
        _request(),
        [SearchResult("https://example.gov/foreign", "City of Foreign")],
    )

    assert result.city_identity == "mismatch"
    assert result.video_platform is None
    assert result.agenda_platform is None
    assert result.candidate_urls == ()
    assert result.video_source is None
    assert result.bodies_mentioned == ()


def test_auxiliary_proposal_needs_agenda_and_primary_video_verification(monkeypatch):
    monkeypatch.setattr("citypods.discovery.verify._signature_verified", lambda *_: True)
    monkeypatch.setattr("citypods.discovery.verify._aux_index_verified", lambda *_: (True, ""))
    monkeypatch.setattr(
        "citypods.discovery.verify._sample_media", lambda *_: ("https://video.example/1", "")
    )
    result = verify_discovery(
        _request("auxiliary"),
        Classification(
            video_platform="swagit",
            agenda_platform="onemeeting",
            candidate_urls=("https://portal.primegov.com/public/portal",),
            bodies_mentioned=("City Council",),
            agenda_source={"portal_url": "https://portal.primegov.com/public/portal"},
        ),
        [SearchResult("https://portal.primegov.com/public/portal")],
        existing_city=SimpleNamespace(
            provider="swagit",
            source={"list_url": "https://x.swagit.com/views"},
            city_entity="example-tx",
            slug="example-tx",
        ),
    )
    assert result.verification.applyable
    assert "aux_provider: onemeeting" in (result.proposed_yaml or "")


def test_rendered_evidence_exposes_controls_and_machine_state(monkeypatch):
    monkeypatch.setattr("citypods.discovery.verify._signature_verified", lambda *_: True)
    monkeypatch.setattr(
        "citypods.discovery.verify._sample_media", lambda *_: ("https://video.example/1", "")
    )
    result = verify_discovery(
        _request(),
        Classification(
            video_platform="granicus",
            agenda_platform=None,
            candidate_urls=("https://example.granicus.com/ViewPublisherRSS.php?view_id=1",),
            bodies_mentioned=(),
            video_source={
                "feed_url": "https://example.granicus.com/ViewPublisherRSS.php?view_id=1"
            },
            city_identity="confirmed",
        ),
        _results(),
    )
    body = render_evidence(result)
    assert "/r12 approve" in body
    assert parse_state_marker(body)["status"] == "proposed"
    assert parse_evidence_marker(body)["request"]["city_slug"] == "example-tx"


def test_foreign_city_evidence_pauses_for_more_information():
    result = verify_discovery(
        _request(),
        Classification(
            video_platform=None,
            agenda_platform=None,
            candidate_urls=(),
            bodies_mentioned=(),
            city_identity="mismatch",
            reasoning="The results identify a city in another state.",
        ),
        [SearchResult("https://foreign.example/meetings", "City of Foreign meetings")],
    )

    body = render_evidence(result)
    assert result.needs_more_information
    assert not result.research_only
    assert result.proposed_yaml is None
    assert "More information needed — discovery paused" in body
    assert "Research finding only" not in body
    assert parse_state_marker(body)["status"] == "needs-more-information"


def test_agenda_covered_city_reenters_after_two_low_coverage_checks():
    city = SimpleNamespace(aux_provider=None, provider="swagit")
    coverage = AgendaCoverage(4, 5, "2026-07-14T00:00:00+00:00")

    assert auxiliary_eligibility(city, coverage, prior_state="agenda-covered", low_checks=1) == (
        "agenda-covered"
    )
    assert auxiliary_eligibility(city, coverage, prior_state="agenda-covered", low_checks=2) == (
        "eligible"
    )
