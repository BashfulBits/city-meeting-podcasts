"""Bounded, single-attempt runtime-payload measurement; never production admission."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import requests
import yaml

from citypods.audit_remedy import (
    P3_ROLE_PLANS,
    BodyDecision,
    P3Adjudication,
    P3Vote,
    _compact_evidence,
    _p3_assignment_key,
    build_p3_review_decision,
)
from citypods.compute.base import JobResult
from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection
from citypods.compute.llm_policy import ROUTE_REGISTRY, estimate_tokens, route_request_params
from citypods.compute.structured_shaping import shape_for_route
from citypods.judging.backends import JevBackend
from citypods.remedy_evaluation import RemedyConfig, canonical_hash, config_fingerprint
from citypods.remedy_p3_runner import (
    P3_CHAT_CONTRACT,
    P3_PROMPT_VERSION,
    P3ChatResponse,
    _adjudication_context,
    _chat_job,
    _jev_answer_for_finding,
    _jev_decision_values,
    _jev_job,
    _response_from_result,
    _validator_passed,
    stable_finding_id,
)
from citypods.security import validate_source_url

ROOT = Path(__file__).resolve().parents[2]
PLAN = P3_ROLE_PLANS["flash-deepseek-v2"]


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp():
    return datetime.now(UTC).isoformat()


def save(path, data):
    with path.open("x") as stream:
        text = json.dumps(data, indent=2)
        for name in (
            "BEATAPI_API_KEY",
            "ORCAROUTER_API_KEY",
            "LLM_DISPATCH_AUTH_TOKEN",
            "LLM_DISPATCH_V2_AUTH_TOKEN",
        ):
            token = os.environ.get(name)
            if token:
                text = text.replace(token, "[REDACTED]")
        stream.write(text + "\n")


def checked(data):
    if data.get("ok") is not True:
        raise RuntimeError("coordination request was not acknowledged")
    return data


def preflight(plan_path, authority_path, output):
    plan, authority = read(plan_path), read(authority_path)
    if (
        authority.get("approved") is not True
        or authority.get("max_provider_attempts") != 48
        or authority.get("max_attempts_per_case_role") != 1
        or authority.get("run_plan_sha256") != digest(plan_path)
        or authority.get("executor_sha256") != digest(__file__)
        or output.resolve() != (ROOT / authority["output_path"]).resolve()
        or not plan.get("expected_outcomes_approved")
    ):
        raise ValueError("missing exact 48-attempt measurement authority")
    for relative, expected in plan["source_sha256"].items():
        if digest(ROOT / relative) != expected:
            raise ValueError(f"reviewed source changed: {relative}")
    directory = plan_path.parent
    for name, key in (
        ("model-inputs.json", "model_inputs_sha256"),
        ("expected-decisions-draft.json", "expected_draft_sha256"),
    ):
        if digest(directory / name) != plan[key]:
            raise ValueError("reviewed packet changed")
    expected_path = directory / "expected-decisions-approved.json"
    if digest(expected_path) != authority.get("approved_expectations_sha256"):
        raise ValueError("approved expectations changed")
    approved = read(expected_path)
    if approved.get("acceptance_sha256") != plan.get("outcome_acceptance_sha256") or not all(
        row.get("approved") is True for row in approved["cases"]
    ):
        raise ValueError("missing reviewed expected outcomes")
    inputs = read(directory / "model-inputs.json")["cases"]
    if len(inputs) != 12 or {c["case_id"] for c in inputs} != {f"RQ{i:02d}" for i in range(1, 13)}:
        raise ValueError("expected exactly twelve approved cases")
    if any(
        r.api_key_env not in {"BEATAPI_API_KEY", "ORCAROUTER_API_KEY"}
        or r.account_id != "primary"
        or not r.free
        for r in (ROUTE_REGISTRY[rid] for rid in PLAN.routes.values())
    ):
        raise ValueError("only the approved free primary routes may be measured")
    output.mkdir(parents=True, exist_ok=True)
    for path in output.glob("*-intent.json"):
        result = output / path.name.replace("-intent.json", "-result.json")
        if not result.exists() or read(result).get("provider_outcome") == "unknown":
            raise RuntimeError("unresolved provider intent; never duplicate an unknown request")
        row = read(result)
        if (
            row.get("http_status") in {400, 401, 402, 403, 404, 429}
            or row.get("status") == "identity_hold"
        ):
            raise RuntimeError("unresolved access/profile/identity hold; no automatic resume")
    return plan, authority, inputs


class Controller:
    def __init__(self, plan, authority, output, client=None):
        self.plan, self.authority, self.output = plan, authority, output
        self.client = client
        self.reason = "review51-runtime-qualification-" + canonical_hash(authority)[:12]
        self.owned = set()
        self.renewed = 0.0

    def verify_sources(self):
        for relative, expected in self.plan["source_sha256"].items():
            if digest(ROOT / relative) != expected:
                raise RuntimeError("frozen runtime/config source changed; stop measurement")

    def receipt(self, name, data):
        save(self.output / f"{name}-{time.time_ns()}.json", data)

    def status(self, provider):
        data = checked(self.client.status(Selection("provider", provider)))
        own_scope = "provider:" + provider
        for pause in data["pauses"]:
            if pause["scope"] in {
                "global",
                own_scope,
                *["route:" + r for r in PLAN.routes.values()],
            }:
                if pause.get("reason") != self.reason:
                    raise RuntimeError("foreign pause; preserve it and make no request")
        return data

    def arm(self, provider):
        self.status(provider)
        self.owned.add(provider)  # Include ambiguous mutations in owned-only cleanup.
        reply = checked(self.client.pause(Selection("provider", provider), 3600, self.reason))
        self.receipt("pause-" + provider, reply)

    def renew(self):
        if time.monotonic() - self.renewed >= 240:
            for provider in sorted(self.owned):
                self.arm(provider)
            self.renewed = time.monotonic()

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.renew()
            time.sleep(min(10, max(0, until - time.monotonic())))

    def coordinate(self):
        for provider in ("beatapi", "orcarouter"):
            self.arm(provider)
        self.renewed = time.monotonic()
        deadline = time.monotonic() + 900
        while any(self.status(p)["in_flight"] for p in self.owned):
            if time.monotonic() >= deadline:
                raise RuntimeError("provider drain did not complete; no inference")
            self.wait(10)
        self.receipt("drained", {p: self.status(p) for p in self.owned})

    def restore(self):
        errors = []
        for provider in sorted(self.owned):
            try:
                status = checked(self.client.status(Selection("provider", provider)))
                if any(
                    p["scope"] == "provider:" + provider and p.get("reason") == self.reason
                    for p in status["pauses"]
                ):
                    self.receipt(
                        "restore-" + provider,
                        checked(self.client.resume(Selection("provider", provider))),
                    )
            except Exception as exc:
                errors.append(dict(provider=provider, exception_class=type(exc).__name__))
        self.receipt("restoration", dict(errors=errors, observed_at=stamp()))

    def call(self, cid, role, payload, recipe_hash, finding_id):
        self.verify_sources()
        key = cid + "--" + role
        result_path = self.output / (key + "-result.json")
        intent_path = self.output / (key + "-intent.json")
        if result_path.exists():
            if intent_path.exists():
                previous = read(intent_path)
                if (
                    previous["payload_sha256"] != canonical_hash(payload)
                    or previous["recipe_hash"] != recipe_hash
                ):
                    raise ValueError("saved result belongs to a different frozen request")
            return read(result_path)
        if intent_path.exists():
            raise RuntimeError("unknown provider request; do not resubmit")
        if len(list(self.output.glob("*-intent.json"))) >= 48:
            raise RuntimeError("approved attempt budget exhausted")
        route = ROUTE_REGISTRY[PLAN.routes[role]]
        ceiling = JevBackend().max_total_tokens if role == "jev" else route.input_context_limit
        if estimate_tokens([{"role": "user", "content": json.dumps(payload)}]) > ceiling:
            row = dict(
                case_id=cid, role=role, status="input_profile_hold", provider_outcome="not_sent"
            )
            save(result_path, row)
            return row
        previous = [
            read(p).get("started_epoch", 0)
            for p in self.output.glob("*-intent.json")
            if read(p).get("provider") == route.provider
        ]
        self.wait(max(0, 65 - (time.time() - max(previous))) if previous else 65)
        self.renew()
        status = self.status(route.provider)
        if status["in_flight"]:
            raise RuntimeError("provider no longer drained")
        if route.route_id in status["routes"]:
            self.receipt(key + "-reservation", checked(self.client.reserve(route.route_id, 1)))
        elif role == "gemini":
            self.receipt(
                key + "-coordination",
                dict(candidate_unregistered=True, coordination="owned_provider_pause"),
            )
        else:
            raise RuntimeError("approved registered route missing; never charge another route")
        endpoint = route.api_base.rstrip("/") + (
            "/systemone" if role == "jev" else "/" + route.chat_path.lstrip("/")
        )
        validate_source_url(endpoint)
        token = os.environ.get(route.api_key_env)
        if not token:
            raise RuntimeError("primary provider credential unavailable")
        save(
            intent_path,
            dict(
                case_id=cid,
                role=role,
                provider=route.provider,
                route_id=route.route_id,
                upstream_model=route.upstream_model,
                endpoint=endpoint,
                payload=payload,
                payload_sha256=canonical_hash(payload),
                started_at=stamp(),
                started_epoch=time.time(),
                recipe_hash=recipe_hash,
                timeout_seconds=720,
                client_retries=0,
                ordinal=len(list(self.output.glob("*-intent.json"))) + 1,
            ),
        )
        row = dict(
            case_id=cid,
            role=role,
            route_id=route.route_id,
            status="transport_hold",
            provider_outcome="unknown",
            started_at=stamp(),
        )
        row.update(
            provider=route.provider,
            requested_model=route.upstream_model,
            output_cap=payload.get("max_tokens"),
            transport="direct-single-attempt-nonstream",
        )
        response = None
        content = bytearray()
        previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
        started = time.monotonic()
        try:
            signal.setitimer(signal.ITIMER_REAL, 720)
            response = requests.post(
                endpoint,
                json=payload,
                headers={"Authorization": "Bearer " + token},
                timeout=(20, 720),
                allow_redirects=False,
                stream=True,
            )
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 16 * 1024 * 1024:
                    raise ValueError("bounded response exceeded 16MiB")
            text = content.decode("utf-8", errors="replace").replace(token, "[REDACTED]")
            row.update(http_status=response.status_code, provider_outcome="response_received")
            try:
                raw = json.loads(text)
            except ValueError:
                raw = dict(non_json_body=text)
            row["raw_response"] = raw
            row["status"] = "http_failure" if response.status_code != 200 else "contract_hold"
            if response.status_code == 200:
                returned = raw.get("model") if isinstance(raw, dict) else None
                row["returned_model"] = returned
                row["identity_matches"] = (
                    returned in self.plan["runtime_bindings"][role]["accepted_returned_model_ids"]
                )
                if not row["identity_matches"]:
                    row["status"] = "identity_hold"
                else:
                    result = JobResult(
                        task="judge",
                        recipe_hash=recipe_hash,
                        model=PLAN.models[role],
                        route_id=route.route_id,
                        upstream_model=route.upstream_model,
                        output=raw,
                    )
                    normalized = _response_from_result(
                        result, role=role, finding_id=finding_id, role_plan=PLAN.name
                    )
                    if role == "jev":
                        answer = _jev_answer_for_finding(normalized, finding_id)
                        choice, a, b = _jev_decision_values(answer)
                        if choice is None or a is None or b is None:
                            raise ValueError("invalid native JEV choice/probabilities")
                    row.update(status="completed", normalized=normalized)
        except Exception as exc:
            row.update(
                exception_class=type(exc).__name__,
                exception_detail=str(exc).replace(token, "[REDACTED]")[:400],
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if response is not None:
                response.close()
            signal.signal(signal.SIGALRM, previous_handler)
            if content and "raw_response" not in row:
                row["partial_response"] = content.decode("utf-8", errors="replace").replace(
                    token, "[REDACTED]"
                )
            row.update(elapsed_seconds=time.monotonic() - started, finished_at=stamp())
            save(result_path, row)
        print(
            json.dumps(
                {
                    k: row.get(k)
                    for k in ("case_id", "role", "status", "http_status", "elapsed_seconds")
                }
            ),
            flush=True,
        )
        if row.get("provider_outcome") == "unknown" or row["status"] == "identity_hold":
            raise RuntimeError("unknown request outcome or identity hold; stop all new requests")
        if row.get("http_status") in {400, 401, 402, 403, 404, 429}:
            raise RuntimeError("provider access/capacity hold; no retry or fallback")
        return row


def timeout_handler(_signum, _frame):
    raise TimeoutError("approved 720-second hard response deadline")


def chat_payload(job, role):
    route = ROUTE_REGISTRY[PLAN.routes[role]]
    messages, fmt = shape_for_route(
        job.inputs["messages"],
        name=P3_CHAT_CONTRACT,
        schema=P3ChatResponse.model_json_schema(),
        route=route,
    )
    payload = dict(
        model=route.upstream_model,
        messages=messages,
        max_tokens=16384,
        stream=False,
        **route_request_params(route),
    )
    if fmt:
        payload["response_format"] = fmt
    return payload


def vote(role, result, fid, eh, ph):
    return P3Vote(
        route_id=PLAN.routes[role],
        upstream_model=result["returned_model"],
        reasoning_level=None,
        evidence_hash=eh,
        policy_hash=ph,
        response_hash=canonical_hash(result["normalized"]),
        decision=BodyDecision.model_validate(result["normalized"]["decision"]).model_copy(
            update={"finding_id": fid}
        ),
    )


def semantic(decision, expected, evidence):
    wanted = BodyDecision.model_validate(expected["decision"])
    if decision.action == "manual_review" and wanted.action != "manual_review":
        return (
            "accepted_manual_fallback"
            if expected.get("outcome_acceptance", {}).get("manual_fallback_accepted")
            else "manual_instead_of_positive"
        )
    if decision.action != wanted.action or set(decision.target_feeds) != set(wanted.target_feeds):
        return "wrong"
    shown = {
        e["episode_id"]
        for e in _compact_evidence(evidence)["unexpected_findings"][0]["episode_samples"]
    }
    if not set(decision.episode_ids) <= shown:
        return "invalid_episode_reference"
    if wanted.action == "new_feed" and not all(
        v.strip()
        for v in (decision.new_feed_slug, decision.new_feed_title, decision.new_feed_description)
    ):
        return "incomplete_new_feed_metadata"
    if wanted.action == "single_uid_inclusion":
        all_ids = {f"e{i}" for i in range(len(evidence["unexpected_findings"][0]["episodes"]))}
        selected = all_ids if decision.all_observed_episodes else set(decision.episode_ids)
        if selected != set(wanted.episode_ids):
            return "wrong"
    if wanted.action == "new_feed" and decision.new_feed_slug != wanted.new_feed_slug:
        return "needs_human_name_review"
    return "correct"


def run_cases(controller, cases, expectations):
    cfg = RemedyConfig.model_validate(yaml.safe_load((ROOT / "config/remedy.yml").read_text()))
    cfg = cfg.model_copy(update={"mode": "manual", "p3_role_plan": PLAN.name})
    for case in cases:
        cid, evidence = case["case_id"], case["evidence"]
        summary_path = controller.output / (cid + "-summary.json")
        if summary_path.exists():
            continue
        fid = stable_finding_id(
            evidence["source_key"], evidence["unexpected_findings"][0]["unexpected_body"]
        )
        eh = canonical_hash(evidence)
        ph = canonical_hash(
            dict(
                config=config_fingerprint(cfg),
                prompt=P3_PROMPT_VERSION,
                remedy_policies=[f["remedy_policy"] for f in evidence["existing_feeds"]],
            )
        )
        results, votes = {}, []
        for role in PLAN.initial:
            frozen = case["jobs"][role]
            results[role] = controller.call(
                cid, role, frozen["direct_measurement_payload"], frozen["recipe_hash"], fid
            )
            if results[role]["status"] == "completed":
                votes.append(vote(role, results[role], fid, eh, ph))
        if len(votes) != 2:
            save(
                summary_path,
                dict(
                    case_id=cid,
                    status="initial_contract_or_availability_hold",
                    semantic_verdicts={
                        r: semantic(vote(r, row, fid, eh, ph).decision, expectations[cid], evidence)
                        for r, row in results.items()
                        if row["status"] == "completed"
                    },
                ),
            )
            continue
        controller.verify_sources()
        validators = {v.route_id: _validator_passed(v.decision, evidence, str(ROOT)) for v in votes}
        adjudication = None
        adjudication_valid = False
        selected = votes[0].decision
        if _p3_assignment_key(votes[0].decision) != _p3_assignment_key(votes[1].decision):
            extra = _adjudication_context(votes, evidence, PLAN.name)
            job = _chat_job(
                role=PLAN.chat,
                role_plan=PLAN.name,
                finding_id=fid,
                evidence_hash=eh,
                policy_hash=ph,
                evidence=evidence,
                extra_context=extra,
            )
            results[PLAN.chat] = controller.call(
                cid, PLAN.chat, chat_payload(job, PLAN.chat), job.recipe_hash, fid
            )
            job, _, _ = _jev_job(
                role_plan=PLAN.name,
                finding_id=fid,
                initial_votes=tuple(votes),
                evidence=evidence,
                evidence_hash=eh,
                policy_hash=ph,
                extra_context=extra,
            )
            route = ROUTE_REGISTRY[PLAN.routes["jev"]]
            results["jev"] = controller.call(
                cid,
                "jev",
                dict(model=route.upstream_model, **job.inputs["systemone"]),
                job.recipe_hash,
                fid,
            )
            if all(results[r]["status"] == "completed" for r in (PLAN.chat, "jev")):
                chat_vote = vote(PLAN.chat, results[PLAN.chat], fid, eh, ph)
                choice, a, b = _jev_decision_values(
                    _jev_answer_for_finding(results["jev"]["normalized"], fid)
                )
                adjudication = P3Adjudication(
                    chat=chat_vote,
                    jev_route_id=route.route_id,
                    jev_upstream_model=results["jev"]["returned_model"],
                    evidence_hash=eh,
                    policy_hash=ph,
                    response_hash=canonical_hash(results["jev"]["normalized"]),
                    choice=choice,
                    supported_a=a,
                    supported_b=b,
                )
                adjudication_valid = _validator_passed(chat_vote.decision, evidence, str(ROOT))
                selected = votes[0 if choice == "A" else 1].decision
        controller.verify_sources()
        decision = build_p3_review_decision(
            finding_id=fid,
            evidence_hash=eh,
            policy_hash=ph,
            initial_votes=votes,
            initial_validators=validators,
            adjudication=adjudication,
            adjudication_validator_passed=adjudication_valid,
            admitted_routes=frozenset(PLAN.routes.values()),
            role_plan=PLAN.name,
        )
        verdicts = {
            r: semantic(vote(r, row, fid, eh, ph).decision, expectations[cid], evidence)
            for r, row in results.items()
            if r != "jev" and row["status"] == "completed"
        }
        save(
            summary_path,
            dict(
                case_id=cid,
                status="measured",
                semantic_verdicts=verdicts,
                consensus_simulation=decision.model_dump(mode="json"),
                simulation_assumes_route_eligibility=True,
                production_admitted=False,
                automatic_candidate_semantic_verdict=semantic(selected, expectations[cid], evidence)
                if decision.disposition == "pr_candidate"
                else None,
            ),
        )
        print(cid + " measured; production remains shadowed", flush=True)


def snapshot(output):
    rows = [read(p) for p in sorted(output.glob("*-result.json"))]
    cases = [read(p) for p in sorted(output.glob("RQ*-summary.json"))]
    return dict(
        observed_at=stamp(),
        provider_attempts=len(list(output.glob("*-intent.json"))),
        responses=len(rows),
        statuses=dict(Counter(r["status"] for r in rows)),
        terminal_cases=len(cases),
        cases=cases,
        production_admitted=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    plan, authority, cases = preflight(args.plan, args.authority, args.output)
    if not args.execute:
        print(
            json.dumps(dict(status="validated_no_network", cases=len(cases), maximum_attempts=48))
        )
        return

    def stop_handler(_signum, _frame):
        raise KeyboardInterrupt("measurement interrupted; do not repeat unknown requests")

    signal.signal(signal.SIGTERM, stop_handler)
    with (args.output / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding = dict(plan_sha256=digest(args.plan), authority_sha256=digest(args.authority))
        if (args.output / "run-binding.json").exists():
            if read(args.output / "run-binding.json") != binding:
                raise ValueError("run directory belongs to a different approved profile")
        else:
            save(args.output / "run-binding.json", binding)
        save(
            args.output / f"controller-start-{time.time_ns()}.json",
            dict(pid=os.getpid(), at=stamp()),
        )
        if not all(os.environ.get(name) for name in ("BEATAPI_API_KEY", "ORCAROUTER_API_KEY")):
            raise RuntimeError("required primary provider credential unavailable")
        controller = Controller(plan, authority, args.output, DispatchPauseClient())
        expectations = {
            c["case_id"]: c
            for c in read(args.plan.parent / "expected-decisions-approved.json")["cases"]
        }
        try:
            controller.coordinate()
            run_cases(controller, cases, expectations)
        except Exception as exc:
            controller.receipt(
                "controller-hold",
                dict(exception_class=type(exc).__name__, detail=str(exc)[:300], at=stamp()),
            )
            raise
        finally:
            controller.restore()
            save(args.output / f"measurement-snapshot-{time.time_ns()}.json", snapshot(args.output))


if __name__ == "__main__":
    main()
