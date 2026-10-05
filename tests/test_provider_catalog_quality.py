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
    session = Session(
        [
            page(1, [row("OpenAI", "gpt-oss-120b", 30)], True),
            page(2, [row("NVIDIA", "nvidia-nemotron-3-super-120b-a12b", 35)]),
        ]
    )
    quality = fetch_quality_index(session)
    assert quality.error is None
    assert quality.floor == 30
    assert quality.score("nvidia/nemotron-3-super-120b-a12b") == 35
    assert AA_URL == "https://artificialanalysis.ai/api/v2/language/models/free"
    assert session.calls == [
        (AA_URL, {"headers": {"x-api-key": "test-key"}, "params": {"page": n}, "timeout": 30})
        for n in (1, 2)
    ]


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
