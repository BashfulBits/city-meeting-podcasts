"""Structured output is shaped per route from the route's method (review/48 R10)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from citypods.compute.base import InferenceJob
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.structured import register_response_model
from citypods.compute.structured_shaping import shape_for_route
from tests.test_compute_llm_dispatch_v2 import MockStorage, _accept_all

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "structured_output_shaping.json").read_text()
)


class _ShapedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


register_response_model("structured-shaping-test", _ShapedOutput)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_python_shaping_matches_the_contract_shared_with_the_worker(case):
    request = case["request"]
    messages, response_format = shape_for_route(
        request["messages"],
        name=request["structured_output"]["name"],
        schema=request["structured_output"]["schema"],
        route=case["route"],
    )
    assert messages == case["expected"]["messages"]
    assert response_format == case["expected"]["response_format"]


def test_the_contract_covers_every_method():
    methods = {case["route"]["structured_output_method"] for case in FIXTURE["cases"]}
    assert methods == {"json_schema", "json_schema_relaxed", "json_object", "prompt_only"}


def test_a_queued_job_carries_only_its_schema_never_a_pre_shaped_request():
    # A pooled job can be served by any route in its pool; only the Worker knows which, so the
    # producer must not pick a response_format (or edit the prompt) for the first model's route.
    storage = MockStorage()
    session = MagicMock()
    session.post.side_effect = _accept_all
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3.1-flash-lite",
            dispatch_v2_url="https://dispatch-v2.example.com",
            dispatch_v2_auth_token="secret",
        ),
        http_session=session,
        storage=storage,
    )
    messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
    backend.enqueue_batch(
        [
            InferenceJob(
                task="tag",
                inputs={
                    "messages": messages,
                    "structured_output": "structured-shaping-test",
                    "max_tokens": 512,
                    "llm_policy": LLMRequestPolicy(
                        allowed_models=(
                            "gemini/gemini-3.1-flash-lite",
                            "deepseek/deepseek-v4.1-flash",
                        ),
                        queue_only=True,
                    ),
                },
                recipe_hash="recipe-schema-only",
            )
        ]
    )
    payload = json.loads(next(v for k, v in storage.files.items() if k.startswith("payloads/")))
    assert "response_format" not in payload
    assert payload["messages"] == messages
    assert payload["structured_output"] == {
        "name": "_ShapedOutput",
        "schema": _ShapedOutput.model_json_schema(),
    }
    assert payload["max_tokens"] == 512


def test_a_direct_call_on_a_prompt_only_route_sends_no_response_format():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        message = MagicMock(content='{"answer": "ok"}')
        return MagicMock(
            choices=[MagicMock(message=message)],
            model_dump=lambda: {
                "choices": [{"message": {"role": "assistant", "content": '{"answer": "ok"}'}}]
            },
        )

    backend = LiteLLMBackend(
        LLMBackendConfig(model="deepseek/deepseek-v4.1-flash"), completion=completion
    )
    backend.run_inference(
        InferenceJob(
            task="tag",
            inputs={
                "messages": [{"role": "user", "content": "hi"}],
                "structured_output": "structured-shaping-test",
                "max_tokens": 64,
            },
            recipe_hash="recipe-direct-prompt-only",
        )
    )
    assert len(calls) == 1
    assert "response_format" not in calls[0]
    assert calls[0]["messages"][0]["role"] == "system"
    assert "matching this JSON Schema" in calls[0]["messages"][0]["content"]
