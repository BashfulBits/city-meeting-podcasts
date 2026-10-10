"""Bounded evaluation diagnostics; defaults never activate or qualify a production model."""

import argparse
import json
import os
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.compute.base import InferenceJob
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection, paused
from citypods.compute.llm_policy import (
    ROUTE_CANDIDATES,
    ROUTE_REGISTRY,
    LLMRequestPolicy,
)
from citypods.compute.llm_policy import (
    ROUTES as LOGICAL_ROUTES,
)
from citypods.compute.structured import parse_structured_json, register_response_model
from citypods.config import load_site_config
from citypods.remedy_evaluation import (
    Manifest,
    RemedyEvalAnswer,
    canonical_hash,
    case_messages,
    prompt_case_id,
    validate_answer,
)
from citypods.storage import make_storage

ROOT = Path(__file__).resolve().parents[4]
ROUTES = {
    "glm": "nvidia_glm_5_3_eval",
    "astra": "beatapi_gpt_6_astra_free",
    "sol": "beatapi_gpt_6_1_sol_free",
    "deepseek": "beatapi_deepseek_v4_1_flash_free",
}


def sanitize(value):
    text = str(value)
    for key, secret in os.environ.items():
        if any(part in key for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and len(secret) > 7:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(key=|bearer\s+)[^\s&]+", r"\1[REDACTED]", text)
    return text[:4000]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=ROUTES)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--retry-from", type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit("Refusing existing output before any provider call")

    route_id = ROUTES[args.model]
    if args.model == "glm":
        template = ROUTE_REGISTRY["nvidia_kimi_k3_free"]
        candidate = replace(
            template,
            model="nvidia/z-ai/glm-5.3",
            route_id=route_id,
            upstream_model="z-ai/glm-5.3",
            direct_model="openai/z-ai/glm-5.3",
            quota=replace(template.quota, rpm=1, rpd=None, tpm=None, concurrency=1),
            input_context_limit=32768,
            output_context_limit=16384,
            hard_input_ceiling=None,
            reasoning_controls_json="",
            request_params_json="",
            structured_output_method="prompt_only",
            structured_output_response_format="none",
            structured_output_include_schema_in_prompt=True,
        )
        ROUTE_REGISTRY[route_id] = candidate
        ROUTE_CANDIDATES[candidate.model] = (candidate,)
        LOGICAL_ROUTES[candidate.model] = candidate
        from citypods.compute import llm

        llm.SUPPORTED_MODELS = llm.SUPPORTED_MODELS | {candidate.model}
    route = ROUTE_REGISTRY[route_id]
    if route.provider == "beatapi":
        route = replace(route, direct_model="openai/" + route.upstream_model)
        ROUTE_REGISTRY[route_id] = route
        ROUTE_CANDIDATES[route.model] = (route,)
        LOGICAL_ROUTES[route.model] = route

    manifest_path = ROOT / "evals/remedy/holdout/p1-denton-2026-10-10/manifest.json"
    manifest = Manifest.model_validate_json(manifest_path.read_text())
    retry_pairs = None
    if args.retry_from:
        previous = json.loads(args.retry_from.read_text())
        if (
            previous.get("model") != args.model
            or previous.get("route_id") != route_id
            or previous.get("manifest_hash") != canonical_hash(manifest.model_dump())
        ):
            raise SystemExit("Retry provenance mismatch before provider calls")
        retry_pairs = {
            (row["mode"], row["case_id"])
            for row in previous["results"]
            if row.get("status") == "failed"
            and row.get("error") == "Production provider did not drain; case deferred"
        }
        completed = {
            (row["mode"], row["case_id"])
            for row in previous["results"]
            if row.get("status") == "completed"
        }
        retry_pairs -= completed
        valid = {
            (mode, case.id)
            for mode in ("claim_support", "blind_owner")
            for case in manifest.cases[:10]
        }
        if not retry_pairs <= valid:
            raise SystemExit("Unknown retry case before provider calls")
    site = load_site_config(ROOT / "config/site_config.yml")
    storage = make_storage(site, site.get("base_url", ""), ROOT / site.get("output_dir", "docs"))
    if storage is None or not storage.cas_capable:
        raise SystemExit("Missing shared CAS quota storage")
    register_response_model("remedy-eval-v2", RemedyEvalAnswer)
    backend = LiteLLMBackend(LLMBackendConfig(model=route.model), storage=storage)
    original = backend._completion_fn()
    rows = []
    diagnostics = []

    def observed_completion(**kwargs):
        record = {"started_at": datetime.now(UTC).isoformat(), "model": kwargs.get("model")}
        diagnostics.append(record)
        started = time.monotonic()
        try:
            response = original(**kwargs)
            record["status"] = "completed"
            return response
        except Exception as exc:
            record.update(status="failed", error_class=type(exc).__name__, error=sanitize(exc))
            raise
        finally:
            record["duration_seconds"] = time.monotonic() - started

    backend._completion = observed_completion
    modes = ["claim_support", "blind_owner"]
    cap = 10
    client = DispatchPauseClient()
    for mode in modes:
        for case in manifest.cases[:cap]:
            if retry_pairs is not None and (mode, case.id) not in retry_pairs:
                continue
            row = {"case_id": case.id, "mode": mode, "qualified": False}
            timeout = 600
            effort = None
            tokens = 16384
            policy = LLMRequestPolicy(
                allowed_models=(route.model,),
                allowed_route_ids=(route_id,),
                purpose="audit-remedy",
                require_direct=True,
                deadline_at=datetime.now(UTC) + timedelta(seconds=650),
            )
            job = InferenceJob(
                task="tag",
                recipe_hash=canonical_hash(
                    {"diagnostic": args.model, "case": case.id, "mode": mode, "tokens": tokens}
                ),
                inputs={
                    "messages": case_messages(case, mode),
                    "structured_output": "remedy-eval-v2",
                    "llm_policy": policy,
                    "reasoning_level": effort,
                    "max_tokens": tokens,
                    "timeout": timeout,
                    "num_retries": 0,
                    "max_provider_attempts": 1,
                },
            )
            started = time.monotonic()
            try:
                with paused(
                    Selection("provider", route.provider),
                    seconds=1200,
                    drain_timeout=120,
                    reason="Maintainer-authorized patient quality evaluation",
                    client=client,
                ) as window:
                    if not window.drained:
                        raise RuntimeError("Production provider did not drain; case deferred")
                    if args.model != "glm":
                        client.reserve(route_id, 1)
                    result = backend.run_immediate(job)
                row.update(
                    raw_response=result.output,
                    route_id=result.route_id,
                    upstream_model=result.upstream_model,
                    reasoning_level=result.reasoning_level,
                )
                returned = result.output.get("model")
                if returned not in {route.upstream_model, route.direct_model}:
                    raise ValueError("Returned model identity mismatch")
                answer = parse_structured_json(result.output["choices"][0]["message"]["content"])
                parsed = validate_answer(
                    answer, case.model_copy(update={"id": prompt_case_id(case, mode)})
                )
                row.update(
                    status="completed",
                    answer=parsed.model_copy(update={"case_id": case.id}).model_dump(),
                )
            except Exception as exc:
                row.update(status="failed", error_class=type(exc).__name__, error=sanitize(exc))
            row["duration_seconds"] = time.monotonic() - started
            rows.append(row)
            checkpoint = args.out.with_name(args.out.stem + f"-{len(rows):02d}.json")
            checkpoint.write_text(
                json.dumps({"results": rows, "diagnostics": diagnostics}, indent=2)
            )
            print(args.model, mode, case.id, row["status"], row.get("error_class"), flush=True)
            if row.get("error_class") in {"BadRequestError", "AuthenticationError"}:
                break
            if route.provider == "beatapi":
                time.sleep(65)
    with args.out.open("x") as output:
        json.dump(
            {
                "kind": "diagnostic-baseline-only",
                "model": args.model,
                "route_id": route_id,
                "manifest_hash": canonical_hash(manifest.model_dump()),
                "qualified": False,
                "retry_parent_hash": canonical_hash(previous) if args.retry_from else None,
                "results": rows,
                "diagnostics": diagnostics,
            },
            output,
            indent=2,
        )


if __name__ == "__main__":
    main()
