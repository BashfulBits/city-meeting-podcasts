"""Freeze, compare and rescore remedy evidence without production mutations."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
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


def run_cases(manifest, config, role, *, dry_run, max_cases, backend=None, mode="claim_support"):
    """Candidates are execution-only: no publication or production-admission API exists here."""
    from citypods.compute.llm_policy import ROUTE_REGISTRY, LLMRequestPolicy

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
        if remaining <= 0 or plan["configuration_id"] in exhausted:
            rows.append({**row, "status": "unattempted", "reason": "bounded run/capacity"})
            continue
        remaining -= 1
        case = next(case for case in manifest.cases if case.id == row["case_id"])
        policy = LLMRequestPolicy(
            allowed_models=tuple(ROUTE_REGISTRY[r].model for r in entry["physical_route_ids"]),
            allowed_route_ids=tuple(entry["physical_route_ids"]),
            purpose="audit-remedy",
            require_direct=True,
            deadline_at=datetime.now(UTC) + timedelta(seconds=60),
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
                "timeout": 30,
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
        row["latency_seconds"] = time.monotonic() - started
        rows.append(row)
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
        value = run_cases(
            manifest,
            config,
            args.role,
            dry_run=args.dry_run,
            max_cases=args.max_cases,
            mode=args.mode,
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
