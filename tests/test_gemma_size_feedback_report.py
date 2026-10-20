from scripts.gemma_size_feedback_report import correlate


def test_verified_pair_preserves_input_total_and_quota_distinctions():
    refusal = {
        "event": "gemma_provider_attempt",
        "job_id": "job",
        "request_digest": "generation",
        "model": "gemma-4-31b-it",
        "provider_status_code": 429,
        "failure_class": "own_tpm",
        "actual_end_at": 10,
        "quota": {
            "violations": [
                {
                    "quota_id": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                    "quota_value": 16000,
                }
            ]
        },
        "raw_error": "PRIVATE ERROR",
    }
    response = {
        "job_id": "job",
        "request_digest": "generation",
        "identity_verified": True,
        "model": "google/gemma-4-31b-it",
        "route_id": "nvidia_gemma",
        "actual_end_at": 20,
        "usage": {"prompt_tokens": 14000, "completion_tokens": 4000, "total_tokens": 18000},
        "choices": "PRIVATE COMPLETION",
    }
    report = correlate([{"source": refusal}], [response])
    assert report["paired_packets"] == 1
    assert report["correlated_size_overflows"] == 1
    assert report["pairs"][0]["combined_only_over_16k"]
    assert "PRIVATE" not in str(report)
    response["usage"]["prompt_tokens"] = 17000
    assert correlate([refusal], [response])["correlated_size_overflows"] == 1
    assert correlate([refusal], [response, response])["paired_packets"] == 1


def test_job_id_alone_wrong_generation_or_model_are_not_sizing_evidence():
    refusal = {
        "event": "gemma_provider_attempt",
        "job_id": "job",
        "request_digest": "old",
        "model": "gemma-4-31b-it",
        "provider_status_code": 429,
        "failure_class": "own_tpm",
        "actual_end_at": 10,
    }
    response = {
        "job_id": "job",
        "request_digest": "new",
        "identity_verified": True,
        "model": "gemma-4-31b-it",
        "route_id": "nvidia_gemma",
        "actual_end_at": 20,
        "usage": {"prompt_tokens": 20000},
    }
    assert correlate([refusal], [response])["paired_packets"] == 0
    response["request_digest"] = "old"
    response["model"] = "gemma-4-26b-a4b-it"
    assert correlate([refusal], [response])["paired_packets"] == 0
    response["model"] = "gemma-4-31b-it"
    response["identity_verified"] = False
    assert correlate([refusal], [response])["paired_packets"] == 0
    del refusal["request_digest"]
    assert correlate([refusal], [response])["excluded"]["google_missing_generation_digest"] == 1


def test_conflicting_generation_responses_are_excluded():
    refusal = {
        "event": "gemma_provider_attempt",
        "job_id": "j",
        "request_digest": "d",
        "provider_status_code": 429,
        "failure_class": "own_tpm",
        "model": "gemma-4-31b-it",
        "actual_end_at": 1,
    }
    response = {
        "job_id": "j",
        "request_digest": "d",
        "identity_verified": True,
        "route_id": "nvidia_gemma",
        "model": "gemma-4-31b-it",
        "actual_end_at": 2,
        "usage": {"prompt_tokens": 14000, "completion_tokens": 4000},
    }
    changed = {**response, "usage": {"prompt_tokens": 10000, "completion_tokens": 1000}}
    report = correlate([refusal], [response, changed])
    assert report["paired_packets"] == 0
    assert report["excluded"]["conflicting_response_generation"] == 2
