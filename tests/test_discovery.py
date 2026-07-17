from __future__ import annotations

from types import SimpleNamespace

import pytest

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import LLMStructuredOutputError
from citypods.discovery.classify import (
    STRUCTURED_OUTPUT,
    CivicPlatformClassificationResponse,
    ClassificationDeferred,
    PlatformSource,
    classify,
    parse_classification,
)
from citypods.discovery.config import discovery_llm_config
from citypods.discovery.eligibility import AgendaCoverage, auxiliary_eligibility
from citypods.discovery.models import (
    KNOWN_PLATFORMS,
    Classification,
    DiscoveryRequest,
    SearchResult,
)
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


def _source_payload(**values: str | None) -> dict[str, str | None]:
    """Return a strict provider-source object with every nullable field present."""
    return {field: values.get(field) for field in PlatformSource.model_fields}


def _classification_content(**overrides: object) -> str:
    """Serialize one valid classifier payload for mocked backend replies."""
    payload: dict[str, object] = {
        "city_identity": "confirmed",
        "video_platform": None,
        "agenda_platform": None,
        "candidate_urls": [],
        "video_source": None,
        "agenda_source": None,
        "bodies_mentioned": [],
        "confidence": "low",
        "reasoning": "test",
    }
    payload.update(overrides)
    return CivicPlatformClassificationResponse.model_validate(payload).model_dump_json()


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


def test_discovery_script_returns_tempfail_for_invalid_structured_output(monkeypatch, capsys):
    monkeypatch.setattr(city_discovery_script, "load_site_config", lambda *_: {"defaults": {}})
    monkeypatch.setattr(
        city_discovery_script, "_request_from_args", lambda _args: (_request(), None)
    )
    monkeypatch.setattr(
        city_discovery_script,
        "TavilyClient",
        lambda: SimpleNamespace(search=lambda _request: _results()),
    )
    monkeypatch.setattr(city_discovery_script, "LiteLLMBackend", lambda *_, **__: None)

    def invalid_response(*_args):
        raise LLMStructuredOutputError("structured LLM response failed Pydantic validation")

    monkeypatch.setattr(city_discovery_script, "classify", invalid_response)

    assert city_discovery_script.main(["--mode", "new-city"]) == city_discovery_script.DEFERRED_EXIT
    assert "discovery deferred" in capsys.readouterr().err


def test_discovery_script_defers_rather_than_crashes_without_cas_storage(monkeypatch, capsys):
    """Regression test: classify() always attaches an llm_policy now, so a real LiteLLMBackend
    without CAS-capable storage (e.g. B2/R2 creds absent, as in a local/manual run) must raise
    something this script's except clause catches -- not crash the whole discovery pass. Uses the
    real LiteLLMBackend/classify path (only Tavily and storage are faked) so it actually exercises
    the code that previously slipped past the except clause's LLMBackendError subclass list."""
    monkeypatch.setattr(city_discovery_script, "load_site_config", lambda *_: {"defaults": {}})
    monkeypatch.setattr(
        city_discovery_script, "_request_from_args", lambda _args: (_request(), None)
    )
    monkeypatch.setattr(
        city_discovery_script,
        "TavilyClient",
        lambda: SimpleNamespace(search=lambda _request: _results()),
    )
    monkeypatch.setattr(city_discovery_script, "make_storage", lambda *_args, **_kwargs: None)

    assert city_discovery_script.main(["--mode", "new-city"]) == city_discovery_script.DEFERRED_EXIT
    assert "discovery deferred" in capsys.readouterr().err


def test_classifier_marks_queued_dispatch_for_deferred_retry():
    class Backend:
        def run_inference(self, job: InferenceJob):
            return JobHandle(
                task=job.task,
                recipe_hash=job.recipe_hash,
                backend="litellm",
                ref="request-1",
            )

    with pytest.raises(ClassificationDeferred, match="queued"):
        classify(Backend(), _request(), _results())


def test_classifier_rejects_source_url_not_in_retrieved_evidence():
    response = {
        "choices": [
            {
                "message": {
                    "content": _classification_content(
                        video_platform="granicus",
                        candidate_urls=[
                            "https://example.granicus.com/ViewPublisherRSS.php?view_id=1"
                        ],
                        video_source=_source_payload(feed_url="https://evil.example/feed"),
                        confidence="high",
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
            assert job.inputs["structured_output"] == STRUCTURED_OUTPUT
            assert job.inputs["llm_policy"].allow_paid is True
            assert job.inputs["llm_policy"].purpose == "city-onboarding"
            assert job.inputs["llm_policy"].deadline_at is not None
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={
                    "choices": [
                        {
                            "message": {
                                "content": _classification_content(
                                    video_platform="granicus",
                                    candidate_urls=[
                                        "https://example.granicus.com/ViewPublisherRSS.php?view_id=1"
                                    ],
                                    video_source=_source_payload(
                                        feed_url=(
                                            "https://example.granicus.com/"
                                            "ViewPublisherRSS.php?view_id=1"
                                        )
                                    ),
                                    confidence="high",
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


def test_auxiliary_prompt_requires_confirmed_city_identity_for_strict_schema():
    class Backend:
        def run_inference(self, job: InferenceJob):
            prompt = job.inputs["messages"][0]["content"]
            assert "In auxiliary mode, set city_identity to confirmed" in prompt
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={
                    "choices": [
                        {"message": {"content": _classification_content(video_platform="swagit")}}
                    ]
                },
            )

    assert classify(Backend(), _request("auxiliary"), _results()).city_identity == "confirmed"


def test_classifier_declares_one_pydantic_contract_per_task():
    class Backend:
        def __init__(self):
            self.calls: list[InferenceJob] = []

        def run_inference(self, job: InferenceJob):
            self.calls.append(job)
            content = _classification_content()
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": content}}]},
            )

    backend = Backend()
    result = classify(backend, _request("auxiliary"), _results())

    assert result.agenda_platform is None
    assert len(backend.calls) == 1
    schema = CivicPlatformClassificationResponse.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["confidence"]["enum"] == ["low", "medium", "high"]
    assert schema["$defs"]["PlatformName"]["enum"] == sorted(KNOWN_PLATFORMS)


def test_classifier_discards_foreign_city_provider_details():
    response = {
        "choices": [
            {
                "message": {
                    "content": _classification_content(
                        city_identity="mismatch",
                        video_platform="civicengage",
                        agenda_platform="civicengage",
                        candidate_urls=["https://example.gov/foreign"],
                        video_source=_source_payload(feed_url="https://example.gov/foreign"),
                        bodies_mentioned=["Foreign City Council"],
                        confidence="medium",
                        reasoning="Evidence is for another city.",
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
