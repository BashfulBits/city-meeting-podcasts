"""Offline contracts for the supported Artificial Analysis Free V2 endpoint."""

import pytest
import requests

from citypods.provider_catalog.quality import AA_METRIC, AA_URL, fetch_quality_index


def row(creator, slug, score):
    return {
        "model_creator": {"id": "publisher-id", "name": creator},
        "slug": slug,
        "evaluations": {AA_METRIC: score},
    }


def page(number, rows, more=False):
    return {
        "tier": "free",
        "intelligence_index_version": 4.3,
        "pagination": {
            "page": number,
            "page_size": 200,
            "total_pages": number + int(more),
            "has_more": more,
        },
        "data": rows,
    }


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = next(self.responses)
        if isinstance(payload, Exception):
            raise payload
        response = requests.Response()
        response.status_code = payload if isinstance(payload, int) else 200
        response.json = lambda: payload
        return response


def test_all_pages_and_v2_creator_names(monkeypatch):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "test-key")
    # The documented endpoint is tried first; a 404 there sends the run to the paginated path.
    session = Session(
        [
            404,
            page(1, [row("OpenAI", "gpt-oss-120b", 30)], True),
            page(2, [row("NVIDIA", "nvidia-nemotron-3-super-120b-a12b", 35)]),
        ]
    )
    quality = fetch_quality_index(session)
    assert quality.error is None
    assert quality.floor == 30
    assert quality.score("nvidia/nemotron-3-super-120b-a12b") == 35
    assert AA_URL == "https://artificialanalysis.ai/api/v2/data/llms/models"
    legacy = "https://artificialanalysis.ai/api/v2/language/models/free"
    headers = {"x-api-key": "test-key"}
    assert session.calls == [
        (AA_URL, {"headers": headers, "params": {}, "timeout": 30}),
        (legacy, {"headers": headers, "params": {"page": 1}, "timeout": 30}),
        (legacy, {"headers": headers, "params": {"page": 2}, "timeout": 30}),
    ]


def test_the_documented_endpoint_is_one_unparameterised_unpaginated_request(monkeypatch):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "test-key")
    payload = {
        "status": 200,
        "prompt_options": {"parallel_queries": 1, "prompt_length": "medium"},
        "data": [
            {
                "id": "m1",
                "slug": "minimax-m3-1-flash",
                "model_creator": {"id": "c1", "name": "MiniMax", "slug": "minimax"},
                "evaluations": {AA_METRIC: 29},
            },
            {  # A model AA has not indexed yet reports null, not a score.
                "id": "m2",
                "slug": "brand-new",
                "model_creator": {"id": "c2", "name": "Acme Labs", "slug": "acme"},
                "evaluations": {AA_METRIC: None},
            },
            {  # The creator slug is the stable identifier even when the display name differs.
                "id": "m3",
                "slug": "gpt-oss-120b",
                "model_creator": {"id": "c3", "name": "Open AI", "slug": "openai"},
                "evaluations": {AA_METRIC: 33},
            },
        ],
    }
    session = Session([payload])
    quality = fetch_quality_index(session)
    assert quality.error is None
    assert session.calls == [
        (AA_URL, {"headers": {"x-api-key": "test-key"}, "params": {}, "timeout": 30})
    ]
    assert quality.score("minimaxai/minimax-m3.1-flash") == 29
    assert quality.score("acme/brand-new") is None
    assert quality.score("openai/gpt-oss-120b") == 33


@pytest.mark.parametrize("failure", [401, 403, 429, requests.Timeout(), {}, page(1, [])])
def test_later_failure_discards_partial_scores(monkeypatch, failure):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "test-key")
    session = Session([page(1, [row("OpenAI", "gpt-oss-120b", 30)], True), failure])
    quality = fetch_quality_index(session)
    assert quality.error
    assert not quality.scores


@pytest.mark.parametrize(
    "creator, namespace",
    [
        ("Mistral AI", "mistralai"),
        ("Moonshot AI", "moonshotai"),
        ("Z AI", "z-ai"),
        ("Alibaba", "qwen"),
    ],
)
def test_publisher_names_and_missing_scores(monkeypatch, creator, namespace):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "test-key")
    quality = fetch_quality_index(
        Session(
            [
                page(
                    1,
                    [
                        row(creator, "model", 0),
                        row(creator, "unknown", None),
                    ],
                )
            ]
        )
    )
    assert quality.score(f"{namespace}/model") == 0
    assert quality.score(f"{namespace}/unknown") is None


def test_missing_key_does_not_request(monkeypatch):
    monkeypatch.delenv("ARTIFICIAL_ANALYSIS_API_KEY", raising=False)
    session = Session([])
    assert fetch_quality_index(session).error
    assert not session.calls


@pytest.mark.parametrize(
    "total_pages, has_more", [(2, False), (1, True), (0, False), (True, False)]
)
def test_contradictory_pagination_is_not_a_complete_catalog(monkeypatch, total_pages, has_more):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "test-key")
    payload = page(1, [row("OpenAI", "gpt-oss-120b", 30)])
    payload["pagination"].update(total_pages=total_pages, has_more=has_more)
    quality = fetch_quality_index(Session([payload]))
    assert quality.error
    assert not quality.scores


def _aa(*rows):
    from citypods.provider_catalog.quality import QualityIndex

    return QualityIndex(
        scores={(c, slug): value for c, slug, _name, value in rows},
        names={(c, slug): name for c, slug, name, _value in rows},
    )


def test_separator_and_display_name_spellings_of_one_model_match():
    index = _aa(("minimax", "minimax-m3-1-flash", "MiniMax-M3.1-Flash", 28.9))
    assert index.score("minimaxai/minimax-m3.1-flash") == 28.9
    assert index.score("minimaxai/MiniMax-M31-Flash") == 28.9


def test_a_compact_match_that_two_aa_models_share_stays_unscored_unless_hf_name_picks_one():
    index = _aa(
        ("acme", "acme-x-1-0", "Acme X1.0", 10.0),
        ("acme", "acme-x10", "Acme X10", 20.0),
    )
    assert index.score("acme/x.10") is None
    assert index.score("acme/x.10", hf_name="Acme-X10") == 20.0


def test_similarity_is_never_applied_only_suggested():
    index = _aa(
        ("mistral", "mistral-large-3", "Mistral Large 3", 20.0),
        ("meta", "muse-spark", "Muse Spark", 40.0),
        ("meta", "llama-4", "Llama 4", 10.0),
    )
    assert index.score("mistralai/mistral-large-4-1t-a52b", creator="mistral") is None
    near = index.near_misses("mistralai/mistral-large-4-1t-a52b", creator="mistral")
    assert [row["slug"] for row in near] == ["mistral-large-3"]
    family = index.near_misses("meta/muse-glimmer-30b", creator="meta")
    assert family[0]["slug"] == "muse-spark" and family[0]["score"] == 40.0
    assert len(index.near_misses("meta/muse-glimmer-30b", creator="meta", limit=1)) == 1


def test_an_accepted_match_beats_every_heuristic_and_ignores_stale_targets():
    index = _aa(("meta", "muse-spark", "Muse Spark", 40.0))
    index.aliases["nvidia/meta/muse-glimmer-30b"] = ("meta", "muse-spark")
    assert index.score("meta/muse-glimmer-30b", key="nvidia/meta/muse-glimmer-30b") == 40.0
    assert index.score("meta/muse-glimmer-30b", key="nvidia/other") is None
    index.aliases["nvidia/meta/gone"] = ("meta", "removed-from-aa")
    assert index.score("meta/gone", key="nvidia/meta/gone") is None
