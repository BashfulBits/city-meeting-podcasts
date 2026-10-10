"""Freeze, compare and rescore remedy evidence without production mutations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from citypods.remedy_evaluation import (
    EVALUATION_MODES,
    Manifest,
    RemedyConfig,
    RemedyEvalAnswer,
    canonical_hash,
    case_messages,
    compare_results,
    freeze_cases,
    load_cases,
    prompt_case_id,
    prompt_for_mode,
    validate_admission,
    validate_answer,
)


def write_new(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def publish_checkpoint(directory: Path, value) -> None:
    """Publish a complete immutable observation, never a partial JSON file."""
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"observation-{uuid.uuid4().hex}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def route_daily_capacity(storage, route, now=None) -> bool:
    """Read-only retry-headroom check; scheduler CAS reservations remain authoritative."""
    from citypods.compute.llm_budget import daily_reset_key, load_llm_budget_cas

    budget, _ = load_llm_budget_cas(storage)
    if route.quota.rpd is None:
        return True
    now = now or datetime.now(UTC)
    ledger = budget.routes.get(route.route_id) or budget.routes.get(route.model)
    used = 0
    if ledger is not None and ledger.requests_day_key == daily_reset_key(
        now, route.quota.reset_timezone
    ):
        used = ledger.requests_day
    if not isinstance(used, int) or used < 0:
        raise ValueError("invalid daily quota usage")
    return route.quota.rpd - used >= 2


def run_cases(
    manifest,
    config,
    role,
    *,
    dry_run,
    max_cases,
    backend=None,
    mode="claim_support",
    request_timeout=30,
    case_deadline=60,
    prior_results=(),
    checkpoint=None,
    quota_preflight=None,
):
    """Candidates are execution-only: no publication or production-admission API exists here."""
    from citypods.compute.llm_policy import ROUTE_REGISTRY, LLMRequestPolicy

    if not 1 <= request_timeout <= 120 or not request_timeout <= case_deadline <= 150:
        raise ValueError("invalid evaluation timeout/deadline bounds")
    entries = [entry for entry in config.admissions if entry.role == role]
    rows, plans = [], []
    holds = []
    context = {
        "manifest_hash": canonical_hash(manifest.model_dump()),
        "prompt_hash": canonical_hash(prompt_for_mode(mode)),
        "schema_hash": canonical_hash(RemedyEvalAnswer.model_json_schema()),
        "catalog_hash": canonical_hash(
            json.loads(
                (
                    Path(__file__).resolve().parents[1] / "citypods/compute/llm_routes.json"
                ).read_text()
            )
        ),
    }
    for entry in entries:
        check = validate_admission(entry, ROUTE_REGISTRY, context, for_evaluation=True)
        if not check.usable:
            holds.extend(check.reasons)
            continue
        for case in manifest.cases:
            plans.append(
                {
                    "case_id": case.id,
                    "prompt_case_id": prompt_case_id(case, mode),
                    "configuration_id": canonical_hash(entry.model_dump()),
                    "candidate_status": entry.status,
                    "admission": entry.model_dump(),
                    "messages": case_messages(case, mode),
                }
            )
    case_order = {case.id: index for index, case in enumerate(manifest.cases)}
    plans.sort(key=lambda plan: case_order[plan["case_id"]])
    if not entries:
        holds.append("policy hold: no configured admissions for role")
    completed = {}
    valid_pairs = {(p["case_id"], p["configuration_id"]): p for p in plans}
    for prior in prior_results:
        if (
            prior.get("dry_run")
            or prior.get("mode") != mode
            or any(prior.get(key) != value for key, value in context.items())
        ):
            raise ValueError("resume context mismatch")
        for row in prior.get("results", []):
            pair = (row.get("case_id"), row.get("configuration_id"))
            if pair not in valid_pairs:
                raise ValueError("unknown resume case/configuration pair")
            if row.get("status") != "completed":
                continue
            plan = valid_pairs[pair]
            case = next(c for c in manifest.cases if c.id == pair[0])
            validate_answer(row["answer"], case)
            if row.get("prompt_case_id") != plan["prompt_case_id"]:
                raise ValueError("resume prompt case mismatch")
            if pair in completed and canonical_hash(completed[pair]) != canonical_hash(row):
                raise ValueError("conflicting completed resume observations")
            completed[pair] = row
    if dry_run:
        return {
            "version": 1,
            "dry_run": True,
            "mode": mode,
            "model_observations": 0,
            "policy_holds": holds,
            "plans": plans,
            "results": [],
        }
    if max_cases is None or max_cases <= 0:
        raise ValueError("live execution requires positive --max-cases")
    from citypods.compute.base import InferenceJob
    from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
    from citypods.compute.structured import parse_structured_json, register_response_model
    from citypods.config import load_site_config
    from citypods.storage import make_storage

    register_response_model("remedy-eval-v2", RemedyEvalAnswer)
    if backend is None and plans:
        root = Path(__file__).resolve().parents[1]
        site = load_site_config(root / "config/site_config.yml")
        storage = make_storage(
            site, site.get("base_url", ""), root / site.get("output_dir", "docs")
        )
        if storage is None or not getattr(storage, "cas_capable", False):
            return {
                "version": 1,
                "dry_run": False,
                "mode": mode,
                "model_observations": 0,
                "policy_holds": holds
                + ["policy hold: configured scheduler storage is not CAS-capable"],
                "results": [
                    {
                        "case_id": plan["case_id"],
                        "configuration_id": plan["configuration_id"],
                        "candidate_status": plan["candidate_status"],
                        "status": "unattempted",
                        "reason": "missing CAS scheduler storage",
                    }
                    for plan in plans
                ],
            }
        first = plans[0]["admission"]
        model = ROUTE_REGISTRY[first["physical_route_ids"][0]].model
        backend = LiteLLMBackend(LLMBackendConfig(model=model), storage=storage)
    if quota_preflight is None and backend is not None and getattr(backend, "storage", None):

        def quota_preflight(route):
            return route_daily_capacity(backend.storage, route)

    remaining = max_cases
    exhausted = set()
    for plan in plans:
        entry = plan["admission"]
        row = {
            "case_id": plan["case_id"],
            "configuration_id": plan["configuration_id"],
            "prompt_case_id": plan["prompt_case_id"],
            "candidate_status": entry["status"],
        }
        pair = (row["case_id"], row["configuration_id"])
        if pair in completed:
            rows.append(completed[pair])
            continue
        if remaining <= 0 or plan["configuration_id"] in exhausted:
            rows.append({**row, "status": "unattempted", "reason": "bounded run/capacity"})
            continue
        if quota_preflight is not None:
            try:
                capacity = all(
                    quota_preflight(ROUTE_REGISTRY[r]) for r in entry["physical_route_ids"]
                )
            except Exception:  # noqa: BLE001 -- quota read must fail closed without secrets
                capacity = False
            if not capacity:
                rows.append({**row, "status": "unattempted", "reason": "daily quota headroom"})
                continue
        remaining -= 1
        case = next(case for case in manifest.cases if case.id == row["case_id"])
        policy = LLMRequestPolicy(
            allowed_models=tuple(ROUTE_REGISTRY[r].model for r in entry["physical_route_ids"]),
            allowed_route_ids=tuple(entry["physical_route_ids"]),
            purpose="audit-remedy",
            require_direct=True,
            deadline_at=datetime.now(UTC) + timedelta(seconds=case_deadline),
        )
        job = InferenceJob(
            task="tag",
            recipe_hash=canonical_hash(plan),
            inputs={
                "messages": plan["messages"],
                "structured_output": "remedy-eval-v2",
                "llm_policy": policy,
                "reasoning_level": entry["reasoning_level"],
                "max_tokens": 2048,
                "timeout": request_timeout,
                "num_retries": 0,
                "max_provider_attempts": 2,
            },
        )
        started = time.monotonic()
        try:
            result = backend.run_immediate(job)
            row.update(
                raw_response=result.output,
                provider_attempts=result.provider_attempts,
                route_id=result.route_id,
                upstream_model=result.upstream_model,
                reasoning_level=result.reasoning_level,
                request_params_hash=result.request_params_hash,
            )
            returned = result.output.get("model")
            routes = [ROUTE_REGISTRY[r] for r in entry["physical_route_ids"]]
            if (
                result.route_id not in entry["physical_route_ids"]
                or result.upstream_model != entry["upstream_model"]
                or result.reasoning_level != entry["reasoning_level"]
                or result.request_params_hash != entry["request_params_hash"]
                or returned
                not in {r.upstream_model for r in routes} | {r.direct_model for r in routes}
            ):
                raise ValueError("unknown/mismatched returned physical model or effort")
            answer = parse_structured_json(result.output["choices"][0]["message"]["content"])
            prompt_case = case.model_copy(update={"id": plan["prompt_case_id"]})
            parsed = validate_answer(answer, prompt_case)
            mapped = parsed.model_copy(update={"case_id": case.id})
            row.update(status="completed", answer=mapped.model_dump())
        except TimeoutError as exc:
            exhausted.add(plan["configuration_id"])
            row.update(
                status="failed",
                error_class=type(exc).__name__,
                provider_attempts=getattr(exc, "provider_attempts", row.get("provider_attempts")),
            )
        except Exception as exc:  # noqa: BLE001 -- provider classes vary; preserve safe failure rows
            row.update(
                status="failed",
                error_class=type(exc).__name__,
                provider_attempts=getattr(exc, "provider_attempts", row.get("provider_attempts")),
            )
        row["request_timeout_seconds"] = request_timeout
        row["case_deadline_seconds"] = case_deadline
        row["latency_seconds"] = time.monotonic() - started
        rows.append(row)
        if checkpoint is not None:
            checkpoint(
                {
                    "version": 1,
                    "dry_run": False,
                    "mode": mode,
                    **context,
                    "results": [row],
                    "model_observations": int("raw_response" in row),
                    "policy_holds": holds,
                }
            )
    return {
        "version": 1,
        "dry_run": False,
        "mode": mode,
        "policy_holds": holds,
        "results": rows,
        "model_observations": sum("raw_response" in row for row in rows),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--evidence", type=Path, required=True)
    freeze.add_argument("--policy-root", type=Path, default=Path("config"))
    freeze.add_argument("--out", type=Path, required=True)
    freeze.add_argument("--split", choices=("regression", "holdout"), default="regression")
    freeze.add_argument("--provenance", required=True)
    freeze.add_argument("--force", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--admission", type=Path, default=Path("config/remedy.yml"))
    run.add_argument("--mode", choices=EVALUATION_MODES, default="claim_support")
    run.add_argument("--role", choices=("proposer", "reviewer", "adjudicator"), required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--live", action="store_true")
    run.add_argument("--max-cases", type=int)
    run.add_argument("--request-timeout", type=int, default=30)
    run.add_argument("--case-deadline", type=int, default=60)
    run.add_argument("--resume-results", type=Path, nargs="+", default=[])
    run.add_argument("--checkpoint-dir", type=Path)
    for command in ("report", "rescore"):
        sub = commands.add_parser(command)
        sub.add_argument("--manifest", type=Path, required=True)
        sub.add_argument("--gold", type=Path, required=True)
        sub.add_argument("--results", type=Path, nargs="+", required=True)
        sub.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists() and (args.command != "freeze" or not args.force):
        parser.error("output exists; raw results/reports are immutable")
    if args.command == "freeze":
        evidence = json.loads(args.evidence.read_text())
        policies = {}
        for path in sorted((args.policy_root / "feeds").glob("*.yml")):
            feed = yaml.safe_load(path.read_text())
            if feed.get("remedy_policy"):
                policies.setdefault(feed.get("city", path.stem), {})[path.stem] = feed[
                    "remedy_policy"
                ]
        value = freeze_cases(evidence, policies, split=args.split, provenance=args.provenance)
        args.out.mkdir(parents=True, exist_ok=True)
        output = args.out / "manifest.json"
        if output.exists():
            if not args.force:
                parser.error("frozen manifest exists; use --force to preserve superseded set")
            prior = json.loads(output.read_text())
            previous = args.out / "superseded" / canonical_hash(prior)
            previous.mkdir(parents=True, exist_ok=True)
            for name in ("manifest.json", "gold.json"):
                source = args.out / name
                if source.exists():
                    source.rename(previous / name)
            value["superseded_hash"] = canonical_hash(prior)
        write_new(output, value)
    elif args.command == "run":
        if not args.dry_run and (not args.live or not args.max_cases or args.max_cases <= 0):
            parser.error("execution requires --live and positive --max-cases; or use --dry-run")
        manifest = Manifest.model_validate(json.loads(args.manifest.read_text()))
        config = RemedyConfig.model_validate(yaml.safe_load(args.admission.read_text()))
        metadata = dict(
            admissions_hash=canonical_hash(config.model_dump()),
            catalog_hash=canonical_hash(
                json.loads(
                    (
                        Path(__file__).resolve().parents[1] / "citypods/compute/llm_routes.json"
                    ).read_text()
                )
            ),
            manifest_hash=canonical_hash(manifest.model_dump()),
            prompt_hash=canonical_hash(prompt_for_mode(args.mode)),
            schema_hash=canonical_hash(RemedyEvalAnswer.model_json_schema()),
            observed_at=datetime.now(UTC).isoformat(),
            commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            working_tree_dirty=bool(
                subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
            ),
        )
        if (
            not 1 <= args.request_timeout <= 120
            or not args.request_timeout <= args.case_deadline <= 150
        ):
            parser.error("timeout must be 1..120; deadline must be timeout..150")
        prior_results = [json.loads(path.read_text()) for path in args.resume_results]
        checkpoint = None
        if args.checkpoint_dir is not None:

            def checkpoint(observation):
                publish_checkpoint(args.checkpoint_dir, {**observation, **metadata})

        value = run_cases(
            manifest,
            config,
            args.role,
            dry_run=args.dry_run,
            max_cases=args.max_cases,
            mode=args.mode,
            request_timeout=args.request_timeout,
            case_deadline=args.case_deadline,
            prior_results=prior_results,
            checkpoint=checkpoint,
        )
        value.update(metadata)
        write_new(args.out, value)
    else:
        dataset = load_cases(args.manifest, args.gold)
        reports = []
        for path in args.results:
            raw = json.loads(path.read_text())
            if raw.get("manifest_hash") != canonical_hash(dataset.manifest.model_dump()):
                parser.error("results manifest hash mismatch")
            groups = {}
            for row in raw["results"]:
                groups.setdefault(row.get("configuration_id", "unspecified"), []).append(row)
            if not groups:
                groups["no-model-observations"] = []
            for configuration_id, rows in groups.items():
                mode = raw.get("mode", "claim_support")
                if raw.get("prompt_hash") != canonical_hash(prompt_for_mode(mode)):
                    parser.error("results mode-specific prompt hash mismatch")
                report = compare_results(rows, dataset.gold, manifest=dataset.manifest, mode=mode)
                reports.append(
                    {
                        "results_path": str(path),
                        "results_hash": canonical_hash(raw),
                        "configuration_id": configuration_id,
                        "mode": mode,
                        "attempt_counts": {
                            "first_attempt_completed": sum(
                                row.get("status") == "completed"
                                and row.get("provider_attempts") == 1
                                for row in rows
                            ),
                            "retried_completed": sum(
                                row.get("status") == "completed"
                                and isinstance(row.get("provider_attempts"), int)
                                and row["provider_attempts"] > 1
                                for row in rows
                            ),
                            "known_provider_attempts": sum(
                                row["provider_attempts"]
                                for row in rows
                                if isinstance(row.get("provider_attempts"), int)
                            ),
                            "unknown_attempt_count": sum(
                                row.get("status") in {"completed", "failed"}
                                and row.get("provider_attempts") is None
                                for row in rows
                            ),
                        },
                        "report": report.model_dump(),
                    }
                )
        write_new(args.out, {"version": 1, "derived": args.command, "reports": reports})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
