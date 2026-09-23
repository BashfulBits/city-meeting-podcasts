from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts import reconcile_provider_routes as reconcile


@dataclass
class _Response:
    status_code: int
    payload: Any
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return self.payload


def _provider() -> dict[str, Any]:
    return {
        "api_base": "https://example.test/v1",
        "chat_path": "/chat/completions",
        "accounts": [{"id": "primary", "api_key_env": "EXAMPLE_KEY"}],
    }


def _route(route_id: str, model: str = "model-a", **extra: Any) -> dict[str, Any]:
    return {
        "route_id": route_id,
        "model": f"example/{model}",
        "provider": "airforce",
        "upstream_model": model,
        "input_context_limit": 100,
        "output_context_limit": 10,
        "account_id": "primary",
        "rpm": 1,
        "free": True,
        **extra,
    }


def _qualified(*_args: Any, **_kwargs: Any) -> reconcile.QualityEvidence:
    return reconcile.QualityEvidence("qualified", "fixture comparison")


def test_free_model_marker_and_successful_canary_add_a_route(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {
        "providers": {"airforce": _provider()},
        "routes": [_route("existing")],
    }

    def get(*_args, **_kwargs):
        return _Response(
            200,
            {
                "data": [
                    {"id": "model-a"},
                    {"id": "model-new", "tier": "free", "context_length": 2048},
                ]
            },
        )

    plan = reconcile.plan_reconciliation(
        raw,
        {"airforce"},
        get=get,
        post=lambda *_a, **_k: _Response(200, {}),
        quality=_qualified,
    )

    assert [route["upstream_model"] for route in plan.additions] == ["model-new"]
    assert not plan.findings


def test_groq_catalog_models_are_free_candidates_for_the_confirmed_account(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {
        "providers": {"groq": _provider()},
        "routes": [
            {
                **_route("existing"),
                "provider": "groq",
                "model": "groq/model-a",
            }
        ],
    }

    plan = reconcile.plan_reconciliation(
        raw,
        {"groq"},
        get=lambda *_a, **_k: _Response(
            200,
            {
                "data": [
                    {"id": "model-a"},
                    {"id": "model-new", "context_window": 2048},
                ]
            },
        ),
        post=lambda *_a, **_k: _Response(200, {}),
        quality=_qualified,
    )

    assert [route["upstream_model"] for route in plan.additions] == ["model-new"]


def test_zero_priced_catalog_labels_are_deterministic_free_evidence():
    zero_text = {"pricing": {"prompt": "0", "completion": "0"}}
    assert reconcile.is_free_evidence("openrouter", "owner/model:free", zero_text) is True
    assert reconcile.is_free_evidence("kilo", "owner/model:free", zero_text) is True
    assert reconcile.is_free_evidence("kilo", "owner/model", zero_text) is False
    assert (
        reconcile.is_free_evidence(
            "orcarouter",
            "owner/model-free",
            {"name": "Owner Model (Free)", "pricing": {"request": "0"}},
        )
        is True
    )


def test_airforce_access_tier_does_not_override_a_paid_model_tier():
    assert reconcile.is_free_evidence("airforce", "free", {"tier": "free"}) is True
    assert (
        reconcile.is_free_evidence("airforce", "paid", {"tier": "paid", "access_tiers": ["free"]})
        is False
    )


def test_mistral_billing_aliases_produce_one_chat_route(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {"providers": {"mistral": _provider()}, "routes": []}
    plan = reconcile.plan_reconciliation(
        raw,
        {"mistral"},
        get=lambda *_a, **_k: _Response(
            200,
            {
                "data": [
                    {
                        "id": "model-latest",
                        "billing_model_name": "model-3-5",
                        "capabilities": {"completion_chat": True},
                        "max_context_length": 2048,
                    },
                    {
                        "id": "model-3-5",
                        "billing_model_name": "model-3-5",
                        "capabilities": {"completion_chat": True},
                        "max_context_length": 2048,
                    },
                    {"id": "embed", "capabilities": {"completion_chat": False}},
                ]
            },
        ),
        post=lambda *_a, **_k: _Response(200, {}),
        quality=_qualified,
    )

    assert [route["upstream_model"] for route in plan.additions] == ["model-3-5"]
    assert "not chat-capable" in "\n".join(plan.observations)


def test_nvidia_canary_uses_a_longer_bounded_timeout(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    timeout: float | None = None
    stream: bool | None = None

    def post(*_args, **kwargs):
        nonlocal stream, timeout
        timeout = kwargs["timeout"]
        stream = kwargs["stream"]
        return _Response(200, {})

    assert reconcile.canary("nvidia", _provider(), "model", post=post).classification == "success"
    assert timeout == 90
    assert stream is True


def test_canary_never_treats_an_access_ambiguous_response_as_model_removal(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")

    probe = reconcile.canary(
        "airforce",
        _provider(),
        "model",
        post=lambda *_args, **_kwargs: _Response(
            404, {}, "The model does not exist or you do not have access to it"
        ),
    )

    assert probe.classification == "entitlement"
    assert probe.summary == "HTTP 404"


def test_canary_requires_a_model_specific_400_or_404_for_removal(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")

    assert (
        reconcile.canary(
            "airforce",
            _provider(),
            "model",
            post=lambda *_args, **_kwargs: _Response(403, {}, "model does not exist"),
        ).classification
        == "inconclusive"
    )
    assert (
        reconcile.canary(
            "airforce",
            _provider(),
            "model",
            post=lambda *_args, **_kwargs: _Response(404, {}, "model does not exist"),
        ).classification
        == "missing"
    )


def test_missing_hugging_face_mapping_is_inconclusive_not_an_addition(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {"providers": {"groq": _provider()}, "routes": []}

    plan = reconcile.plan_reconciliation(
        raw,
        {"groq"},
        get=lambda *_a, **_k: _Response(
            200, {"data": [{"id": "unqualified", "context_window": 1}]}
        ),
        post=lambda *_a, **_k: _Response(200, {}),
    )

    assert not plan.additions
    assert "quality evidence inconclusive" in plan.findings[0]


def test_hugging_face_repository_uses_only_reviewed_publisher_aliases():
    assert reconcile._hf_repository("z-ai/glm-5.3", {}) == "zai-org/glm-5.3"
    assert reconcile._hf_repository("unmapped/model", {}) == "unmapped/model"
    assert reconcile._artificial_analysis_identity("z-ai/glm-5.3", {}) == ("zai", "glm-5-3")
    assert reconcile._artificial_analysis_identity(
        "mistral-medium-3-5", {}, provider="mistral"
    ) == ("mistral", "mistral-medium-3-5")
    assert reconcile._artificial_analysis_identity("google/gemma-4-31b-it", {}) == (
        "google",
        "gemma-4-31b",
    )
    assert reconcile._artificial_analysis_identity("qwen/qwen3.8-27b", {}) == (
        "alibaba",
        "qwen3-8-27b",
    )
    assert reconcile._artificial_analysis_identity("moonshotai/kimi-k3", {}) == ("kimi", "kimi-k3")
    assert reconcile._artificial_analysis_identity(
        "qwen/qwen3.8-27b:free", {}, provider="openrouter"
    ) == ("alibaba", "qwen3-8-27b")


def test_artificial_analysis_identity_uses_provider_declared_billing_aliases():
    identities = reconcile._artificial_analysis_identities(
        "mistral-medium-latest",
        {
            "owned_by": "mistralai",
            "billing_model_name": "mistral-medium-3-5",
            "aliases": ["mistral-medium-3.5"],
        },
        provider="mistral",
    )

    assert ("mistral", "mistral-medium-3-5") in identities


def _artificial_analysis_catalog(
    candidate_score: float | None = 45,
) -> reconcile.ArtificialAnalysisCatalog:
    candidate_evaluations: dict[str, float] = {}
    if candidate_score is not None:
        candidate_evaluations[reconcile.ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC] = candidate_score
    return reconcile.ArtificialAnalysisCatalog(
        models=[
            {
                "slug": "candidate",
                "model_creator": {"slug": "owner"},
                "evaluations": candidate_evaluations,
            },
            {
                "slug": "gpt-oss-120b",
                "model_creator": {"slug": "openai"},
                "evaluations": {reconcile.ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC: 39},
            },
            {
                "slug": "nvidia-nemotron-3-super-120b-a12b",
                "model_creator": {"slug": "nvidia"},
                "evaluations": {reconcile.ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC: 39},
            },
        ]
    )


def test_artificial_analysis_gate_qualifies_an_exact_model_above_quality_floor():
    evidence = reconcile.quality_evidence(
        "any-provider",
        "owner/candidate",
        {},
        artificial_analysis=_artificial_analysis_catalog(45),
    )

    assert evidence.classification == "qualified"
    assert "45 >= GPT-OSS-120B / Nemotron-3 Super floor 39" in evidence.summary


def test_artificial_analysis_match_allows_only_versioned_release_build_suffixes():
    catalog = reconcile.ArtificialAnalysisCatalog(
        models=[
            {
                "slug": "model-3",
                "model_creator": {"slug": "owner"},
                "evaluations": {},
            },
            {
                "slug": "model",
                "model_creator": {"slug": "owner"},
                "evaluations": {},
            },
        ]
    )

    assert reconcile._artificial_analysis_model(catalog, ("owner", "model-3-2508")) == {
        "slug": "model-3",
        "model_creator": {"slug": "owner"},
        "evaluations": {},
    }
    assert reconcile._artificial_analysis_model(catalog, ("owner", "model-2603")) is None


def test_reconciliation_fetches_artificial_analysis_only_once(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    monkeypatch.setenv(reconcile.ARTIFICIAL_ANALYSIS_API_KEY_ENV, "not-a-real-key")
    raw = {"providers": {"airforce": _provider()}, "routes": []}
    artificial_analysis_calls = 0

    def get(url: str, **_kwargs: Any) -> _Response:
        nonlocal artificial_analysis_calls
        if url == reconcile.ARTIFICIAL_ANALYSIS_API_URL:
            artificial_analysis_calls += 1
            return _Response(
                200,
                {
                    "data": [
                        {
                            "slug": "model-new",
                            "model_creator": {"slug": "owner"},
                            "evaluations": {reconcile.ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC: 45},
                        },
                        {
                            "slug": "gpt-oss-120b",
                            "model_creator": {"slug": "openai"},
                            "evaluations": {reconcile.ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC: 39},
                        },
                        {
                            "slug": "nvidia-nemotron-3-super-120b-a12b",
                            "model_creator": {"slug": "nvidia"},
                            "evaluations": {reconcile.ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC: 39},
                        },
                    ]
                },
            )
        if url == "https://example.test/v1/models":
            return _Response(
                200,
                {"data": [{"id": "owner/model-new", "tier": "free", "context_length": 2048}]},
            )
        raise AssertionError(url)

    plan = reconcile.plan_reconciliation(
        raw,
        {"airforce"},
        get=get,
        post=lambda *_args, **_kwargs: _Response(200, {}),
    )

    assert [route["upstream_model"] for route in plan.additions] == ["owner/model-new"]
    assert artificial_analysis_calls == 1


def test_artificial_analysis_gate_rejects_an_exact_model_below_gemma():
    evidence = reconcile.quality_evidence(
        "any-provider",
        "owner/candidate",
        {},
        artificial_analysis=_artificial_analysis_catalog(38),
    )

    assert evidence.classification == "rejected"


def test_artificial_analysis_gate_requires_an_exact_numeric_score():
    evidence = reconcile.quality_evidence(
        "any-provider",
        "owner/candidate",
        {},
        artificial_analysis=_artificial_analysis_catalog(None),
        get=lambda *_args, **_kwargs: _Response(200, {}),
    )

    assert evidence.classification == "inconclusive"
    assert "Artificial Analysis has no comparable exact-model score" in evidence.summary


def test_hugging_face_fallback_accepts_only_verified_direction_safe_results():
    def evaluation(value: float, verified: bool) -> dict[str, Any]:
        return {
            "evalResults": [
                {
                    "verified": verified,
                    "data": {
                        "dataset": {"id": "TIGER-Lab/MMLU-Pro", "task_id": "mmlu_pro"},
                        "value": value,
                    },
                }
            ]
        }

    def get(url: str, **_kwargs: Any) -> _Response:
        if "/owner/candidate?" in url:
            return _Response(200, evaluation(90, True))
        if f"/{reconcile.GEMMA_QUALITY_BASELINE}?" in url:
            return _Response(200, evaluation(85, True))
        raise AssertionError(url)

    evidence = reconcile.quality_evidence(
        "any-provider",
        "owner/candidate",
        {},
        get=get,
        artificial_analysis=reconcile.ArtificialAnalysisCatalog(error="fixture unavailable"),
    )

    assert evidence.classification == "qualified"


def test_hugging_face_fallback_rejects_unverified_results():
    evidence = reconcile.quality_evidence(
        "any-provider",
        "owner/candidate",
        {},
        get=lambda *_args, **_kwargs: _Response(
            200,
            {
                "evalResults": [
                    {
                        "verified": False,
                        "data": {
                            "dataset": {"id": "TIGER-Lab/MMLU-Pro", "task_id": "mmlu_pro"},
                            "value": 100,
                        },
                    }
                ]
            },
        ),
        artificial_analysis=reconcile.ArtificialAnalysisCatalog(error="fixture unavailable"),
    )

    assert evidence.classification == "inconclusive"


def test_nvidia_free_scraper_uses_the_matching_build_catalog_result():
    urls: list[str] = []

    def get(url: str, **_kwargs: Any):
        urls.append(url)
        return _Response(
            200,
            {},
            '<li><div data-testid="nv-card-root">example-model...Free Endpoint</div></li>',
        )

    assert reconcile._nvidia_free("nvidia/example-model", get=get) is True
    assert urls == ["https://build.nvidia.com/models?q=example-model"]


def test_nvidia_free_scraper_treats_absent_catalog_result_as_not_free():
    assert (
        reconcile._nvidia_free(
            "nvidia/example-model",
            get=lambda *_a, **_k: _Response(
                200,
                {},
                '<li><div data-testid="nv-card-root">Free Endpoint...another-model</div></li>',
            ),
        )
        is False
    )


def test_nvidia_catalog_scraper_reads_the_free_endpoint_label_once():
    catalog = reconcile.fetch_nvidia_build_catalog(
        get=lambda *_args, **_kwargs: _Response(
            200,
            {
                "results": [
                    {
                        "resources": [
                            {
                                "orgName": reconcile.NVIDIA_BUILD_ORGANIZATION,
                                "name": "glm-5-3",
                                "labels": [
                                    {"key": "publisher", "values": ["z-ai"]},
                                    {"key": "general", "values": ["Free Endpoint"]},
                                ],
                            },
                            {
                                "orgName": reconcile.NVIDIA_BUILD_ORGANIZATION,
                                "name": "paid-model",
                                "labels": [
                                    {"key": "publisher", "values": ["z-ai"]},
                                    {"key": "general", "values": ["Partner Endpoint"]},
                                ],
                            },
                        ]
                    }
                ]
            },
        )
    )

    assert catalog.free_by_model == {"z-ai/glm-5-3": True, "z-ai/paid-model": False}
    assert reconcile.is_free_evidence("nvidia", "z-ai/glm-5.3", {}, nvidia_catalog=catalog) is True


def test_nvidia_model_detail_extracts_only_a_published_context_limit():
    limits = reconcile.nvidia_catalog_limits(
        "z-ai/glm-5.3",
        get=lambda *_args, **_kwargs: _Response(
            200,
            {
                "artifact": {
                    "description": (
                        "The model uses sparse attention over a 1,048,576-token context."
                    )
                }
            },
        ),
    )

    assert limits == {"context_length": 1_048_576}


def test_nvidia_free_scraper_retries_empty_accepted_search(monkeypatch):
    responses = iter(
        [
            _Response(202, {}, ""),
            _Response(202, {}, ""),
            _Response(
                200,
                {},
                '<li><div data-testid="nv-card-root">example-model Free Endpoint</div></li>',
            ),
        ]
    )
    monkeypatch.setattr(reconcile.time, "sleep", lambda _seconds: None)

    assert reconcile._nvidia_free(
        "nvidia/example-model", get=lambda *_args, **_kwargs: next(responses)
    )


def test_nvidia_checks_free_candidates_beyond_the_canary_cap(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {"providers": {"nvidia": _provider()}, "routes": []}

    def get(url: str, **_kwargs: Any) -> _Response:
        if url == "https://example.test/v1/models":
            return _Response(
                200,
                {
                    "data": [
                        {"id": "a/paid", "context_length": 2048},
                        {"id": "b/also-paid", "context_length": 2048},
                        {"id": "z-ai/free-model", "context_length": 2048},
                    ]
                },
            )
        if url.startswith(reconcile.NVIDIA_BUILD_CATALOG_API):
            return _Response(
                200,
                {
                    "results": [
                        {
                            "resources": [
                                {
                                    "orgName": reconcile.NVIDIA_BUILD_ORGANIZATION,
                                    "name": "free-model",
                                    "labels": [
                                        {"key": "publisher", "values": ["z-ai"]},
                                        {"key": "general", "values": ["Free Endpoint"]},
                                    ],
                                }
                            ]
                        }
                    ]
                },
            )
        if url.endswith("q=free-model"):
            return _Response(
                200,
                {},
                '<li><div data-testid="nv-card-root">free-model Free Endpoint</div></li>',
            )
        return _Response(200, {}, '<li><div data-testid="nv-card-root">paid</div></li>')

    plan = reconcile.plan_reconciliation(
        raw,
        {"nvidia"},
        get=get,
        post=lambda *_a, **_k: _Response(200, {}),
        quality=_qualified,
    )

    assert [route["upstream_model"] for route in plan.additions] == ["z-ai/free-model"]


def test_absent_model_needs_explicit_not_found_before_removal(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {"providers": {"airforce": _provider()}, "routes": [_route("missing")]}

    plan = reconcile.plan_reconciliation(
        raw,
        {"airforce"},
        get=lambda *_a, **_k: _Response(200, {"data": []}),
        post=lambda *_a, **_k: _Response(404, {}, "model_not_found"),
    )

    assert plan.removals == {"missing"}
    assert "explicit replacement model is required" in plan.findings[-1]


def test_entitlement_error_preserves_absent_route_and_opens_finding(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {"providers": {"airforce": _provider()}, "routes": [_route("missing")]}

    plan = reconcile.plan_reconciliation(
        raw,
        {"airforce"},
        get=lambda *_a, **_k: _Response(200, {"data": []}),
        post=lambda *_a, **_k: _Response(429, {}, "Insufficient balance or no resource package"),
    )

    assert not plan.removals
    assert "entitlement" in plan.findings[0]


def test_end_of_life_response_is_an_explicit_removal_signal(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {"providers": {"airforce": _provider()}, "routes": [_route("retired")]}

    plan = reconcile.plan_reconciliation(
        raw,
        {"airforce"},
        get=lambda *_a, **_k: _Response(200, {"data": []}),
        post=lambda *_a, **_k: _Response(410, {}, "This model reached its end of life"),
    )

    assert plan.removals == {"retired"}


def test_exact_logical_sibling_avoids_cross_model_backfill_request(monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "not-a-real-key")
    raw = {
        "providers": {"airforce": _provider()},
        "routes": [
            _route("missing", model="retired", model_key="logical/model"),
            _route("sibling", model="still-live", model_key="logical/model"),
        ],
    }

    plan = reconcile.plan_reconciliation(
        raw,
        {"airforce"},
        get=lambda *_a, **_k: _Response(200, {"data": [{"id": "still-live"}]}),
        post=lambda *_a, **_k: _Response(404, {}, "invalid model"),
    )

    assert plan.removals == {"missing"}
    assert not any("logical/model" in finding for finding in plan.findings)


def test_plan_digest_is_stable_when_only_diagnostics_or_quality_timestamp_change():
    baseline = reconcile.Plan(
        additions=[{"route_id": "add", "_quality_comment": "dated quality snapshot"}],
        findings=["temporary upstream failure"],
        removals={"remove"},
    )
    repeat = reconcile.Plan(
        additions=[{"route_id": "add", "_quality_comment": "newer quality snapshot"}],
        findings=["different temporary upstream failure"],
        removals={"remove"},
    )

    assert baseline.digest() == repeat.digest()


def test_sync_issue_closes_the_rolling_issue_only_after_an_apply_run(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(reconcile, "_existing_issue", lambda: "42")
    monkeypatch.setattr(
        reconcile,
        "_run",
        lambda args, **_kwargs: calls.append(args) or SimpleNamespace(returncode=0, stdout=""),
    )

    assert reconcile.sync_issue(reconcile.Plan(), apply=False) is None
    assert not calls
    assert reconcile.sync_issue(reconcile.Plan(), apply=True) is None
    assert calls == [
        [
            "gh",
            "issue",
            "close",
            "42",
            "--comment",
            "No inconclusive provider-catalog evidence remains.",
        ]
    ]


def test_reused_digest_branch_skips_duplicate_route_edits_and_compilation(
    monkeypatch, tmp_path: Path, capsys
):
    limits = tmp_path / "provider_limits.yml"
    limits.write_text("routes:\n", encoding="utf-8")
    args = SimpleNamespace(
        apply=True,
        open_pr=True,
        provider=[],
        sync_issues=False,
    )
    plan = reconcile.Plan(additions=[{"route_id": "catalog-route"}])
    monkeypatch.setattr(reconcile, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(reconcile, "LIMITS_PATH", limits)
    monkeypatch.setattr(reconcile, "parse_args", lambda _argv: args)
    monkeypatch.setattr(reconcile, "plan_reconciliation", lambda *_args: plan)
    monkeypatch.setattr(reconcile, "checkout_pr_branch", lambda _plan: ("existing-branch", True))
    monkeypatch.setattr(
        reconcile,
        "apply_route_changes",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not reapply existing changes")),
    )
    monkeypatch.setattr(reconcile, "_existing_pr", lambda branch: f"https://example.test/{branch}")

    assert reconcile.main([]) == 0
    assert limits.read_text(encoding="utf-8") == "routes:\n"
    output = capsys.readouterr().out
    assert "reconciliation plan: 1 additions, 0 removals, 0 inconclusives" in output
    assert "catalog-route" not in output


def test_targeted_route_edit_preserves_unrelated_comments(tmp_path: Path):
    path = tmp_path / "provider_limits.yml"
    path.write_text(
        "routes:\n"
        "  # keep this explanation\n"
        "  - route_id: remove-me\n"
        "    model: example/remove\n"
        "  # this comment belongs to the survivor\n"
        "  - route_id: keep-me\n"
        "    model: example/keep\n",
        encoding="utf-8",
    )
    plan = reconcile.Plan(removals={"remove-me"})

    reconcile.apply_route_changes(path, plan)

    updated = path.read_text(encoding="utf-8")
    assert "remove-me" not in updated
    assert "keep-me" in updated
    assert "this comment belongs to the survivor" in updated


def test_catalog_addition_renders_aa_evidence_as_comment_only(tmp_path: Path):
    path = tmp_path / "provider_limits.yml"
    path.write_text("routes:\n", encoding="utf-8")
    plan = reconcile.Plan(
        additions=[
            {
                "route_id": "catalog-route",
                "model": "example/model",
                "provider": "example",
                "_quality_comment": "AA Intelligence Index 22.6 >= admission floor 11.6",
            }
        ]
    )

    reconcile.apply_route_changes(path, plan)

    updated = path.read_text(encoding="utf-8")
    assert "# Quality (informational): AA Intelligence Index 22.6" in updated
    assert "_quality_comment" not in updated
