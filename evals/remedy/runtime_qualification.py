"""Prepare a reviewable runtime-contract challenge offline; no execution or credentials."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import yaml

from citypods.audit_remedy import (
    P3_ROLE_PLANS,
    BodyDecision,
    BodyDecisions,
    _compact_evidence,
    _resolve_decisions,
    feed_paths_by_slug,
    validate_proposals,
)
from citypods.compute.llm_policy import ROUTE_REGISTRY, route_request_params
from citypods.compute.structured_shaping import shape_for_route
from citypods.config import load_city_configs, load_site_config
from citypods.records import source_key
from citypods.remedy_evaluation import RemedyConfig, canonical_hash, config_fingerprint
from citypods.remedy_p3_runner import (
    P3_CHAT_CONTRACT,
    P3_PROMPT_VERSION,
    P3ChatResponse,
    _chat_job,
    p3_runtime_bindings,
    stable_finding_id,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "evals/remedy/qualification/runtime-v1/cases-draft.json"
INVENTORY = ROOT / "review/evidence/historical-selector-replay-2026-10-06.csv"
COHORT = ROOT / "evals/remedy/holdout/body-selection-2026-10-13/manifest.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_key(guid):
    """Keep mirrored view URLs visible in review, but count each native clip once."""
    query = parse_qs(urlparse(guid).query)
    return "clip:" + query["clip_id"][0] if query.get("clip_id") else guid


def retained_rows(spec, inventory, cohort):
    if spec["source"] == "inventory":
        return [
            dict(
                uid=r["uid"],
                provider_guid=r["provider_guid"],
                body=r["record_body"],
                title=r["record_title"],
                published=r["record_date"],
                source_key=r["source_key"],
            )
            for r in inventory
            if r["city"] == spec["city"] and r["record_body"] == spec["label"]
        ]
    return [
        dict(c["recordings"][0], published=c["recordings"][0]["date"], source_key=c["source_key"])
        for c in cohort
        if c["city"] == spec["city"] and c["body_label"] == spec["label"]
    ]


def expected_check(decision, evidence):
    if decision.action == "manual_review":
        return dict(status="manual_by_design", reasons=[])
    resolved = _resolve_decisions(
        BodyDecisions(proposals=[decision]), evidence, _compact_evidence(evidence)
    )
    validated = validate_proposals(resolved, evidence, feed_paths_by_slug(ROOT))
    reasons = list(resolved.unresolved.values()) + [p.reason for p in validated.rejected]
    return dict(
        status="passes" if len(validated.accepted) == 1 and not reasons else "held", reasons=reasons
    )


def apply_acceptance(path, spec_path, output, expected):
    """Attach the reviewed case decision without granting any inference authority."""
    acceptance = json.loads(path.read_text())
    actual = {row["case_id"] for row in expected}
    if (
        set(acceptance["approved_case_ids"]) != actual
        or len(acceptance["approved_case_ids"]) != len(actual)
        or acceptance["approved_spec_sha256"] != sha(spec_path)
        or acceptance["reviewed_model_inputs_sha256"] != sha(output / "model-inputs.json")
        or acceptance["reviewed_expected_draft_sha256"]
        != sha(output / "expected-decisions-draft.json")
    ):
        raise ValueError("outcome acceptance does not match the exact reviewed packet")
    approved = []
    for row in expected:
        approved.append(
            {
                **row,
                "approved": True,
                "outcome_acceptance": acceptance["case_dispositions"].get(row["case_id"], {}),
            }
        )
    write(
        output / "expected-decisions-approved.json",
        dict(
            status="expected_outcomes_approved_only",
            acceptance_sha256=sha(path),
            cases=approved,
            call_budget_approved=False,
            runtime_qualified=False,
        ),
    )
    return acceptance


def build(spec_path, output, acceptance_path=None):
    spec = json.loads(spec_path.read_text())
    assert spec["status"] == "draft_not_approved"
    for path, expected in spec["inputs"].items():
        assert sha(ROOT / path) == expected, f"Frozen reference changed: {path}"
    output.mkdir(parents=True, exist_ok=False)
    cohort = json.loads(COHORT.read_text())["cases"]
    with INVENTORY.open() as stream:
        inventory = list(csv.DictReader(stream))
    old_keys = {(c["source_key"], native_key(c["recordings"][0]["provider_guid"])) for c in cohort}
    feeds = load_city_configs(ROOT / "config", load_site_config(ROOT / "config/site_config.yml"))
    config = RemedyConfig.model_validate(yaml.safe_load((ROOT / "config/remedy.yml").read_text()))
    assert config.mode == "shadow" and not config.admissions
    config = config.model_copy(update={"mode": "manual", "p3_role_plan": "flash-deepseek-v2"})
    plan = P3_ROLE_PLANS[config.p3_role_plan]
    bindings = {}
    for role, route_id in plan.routes.items():
        route = ROUTE_REGISTRY[route_id]
        entry = SimpleNamespace(
            physical_route_ids=[route_id],
            upstream_model=route.upstream_model,
            request_params_hash=canonical_hash(route_request_params(route)),
        )
        bindings[role] = p3_runtime_bindings(config, entry)
    inputs, expected, cards, sources = [], [], [], dict(spec["inputs"])
    sources[str(Path(__file__).relative_to(ROOT))] = sha(Path(__file__))
    for name in (
        "citypods/remedy_p3_runner.py",
        "citypods/audit_remedy.py",
        "config/remedy.yml",
        "citypods/compute/llm_routes.json",
    ):
        sources[name] = sha(ROOT / name)
    for case in spec["cases"]:
        rows = retained_rows(case, inventory, cohort)
        assert rows, case["id"]
        source = rows[0]["source_key"]
        assert all(r["source_key"] == source for r in rows)
        unique = {}
        for row in sorted(rows, key=lambda r: (r["published"], r["provider_guid"], r["uid"])):
            unique.setdefault(native_key(row["provider_guid"]), row)
        episodes = list(unique.values())
        related = [f for f in feeds if source_key(f) == source]
        assert related and all(f.city_entity == case["city"] for f in related)
        city_path = ROOT / "config/cities" / (case["city"] + ".yml")
        city = yaml.safe_load(city_path.read_text())
        sources[str(city_path.relative_to(ROOT))] = sha(city_path)
        taxonomy = []
        one_offs = set()
        for feed in related:
            path = ROOT / "config/feeds" / (feed.slug + ".yml")
            sources[str(path.relative_to(ROOT))] = sha(path)
            taxonomy.append(
                dict(
                    slug=feed.slug,
                    podcast_title=feed.podcast_title,
                    body=feed.source.get("body"),
                    body_any=feed.source.get("body_any", []),
                    body_exact=feed.source.get("body_exact", []),
                    remedy_policy=feed.extra.get("remedy_policy", {}),
                )
            )
            for inc in feed.source.get("body_includes", []):
                if inc.get("body") == case["label"]:
                    one_offs.add(case["label"])
        finding = dict(
            unexpected_body=case["label"],
            count=len(episodes),
            date_range=dict(earliest=episodes[0]["published"], latest=episodes[-1]["published"]),
            episodes=[
                {k: r[k] for k in ("provider_guid", "published", "title", "body")} for r in episodes
            ],
            existing_one_off_labels=sorted(one_offs),
        )
        evidence = dict(
            source_key=source,
            city=dict(
                slug=case["city"],
                name=city.get("name", case["city"]),
                state=city.get("state", ""),
                website=city.get("city_website", ""),
                meetings_url=city.get("meetings_url", ""),
            ),
            existing_feeds=taxonomy,
            historical_archive={},
            unexpected_findings=[finding],
        )
        decision = BodyDecision.model_validate(case["expected_decision"])
        check = expected_check(decision, evidence)
        expected.append(
            dict(
                case_id=case["id"],
                decision=decision.model_dump(mode="json"),
                validator=check,
                approved=False,
            )
        )
        policy_hash = canonical_hash(
            dict(
                config=config_fingerprint(config),
                prompt=P3_PROMPT_VERSION,
                remedy_policies=[f["remedy_policy"] for f in taxonomy],
            )
        )
        jobs = {}
        for role in plan.initial:
            job = _chat_job(
                role=role,
                role_plan=plan.name,
                finding_id=stable_finding_id(source, case["label"]),
                evidence_hash=canonical_hash(evidence),
                policy_hash=policy_hash,
                evidence=evidence,
            )
            route = ROUTE_REGISTRY[plan.routes[role]]
            wire_messages, response_format = shape_for_route(
                job.inputs["messages"],
                name=P3_CHAT_CONTRACT,
                schema=P3ChatResponse.model_json_schema(),
                route=route,
            )
            payload = dict(
                model=route.upstream_model,
                messages=wire_messages,
                max_tokens=16384,
                stream=False,
                **route_request_params(route),
            )
            if response_format:
                payload["response_format"] = response_format
            jobs[role] = dict(
                direct_measurement_payload=payload,
                task=job.task,
                recipe_hash=job.recipe_hash,
                inputs={**job.inputs, "llm_policy": asdict(job.inputs["llm_policy"])},
            )
        inputs.append(dict(case_id=case["id"], evidence=evidence, jobs=jobs))
        overlap = sum((source, native_key(r["provider_guid"])) in old_keys for r in episodes)
        cards.append(
            dict(
                spec=case,
                rows=rows,
                episodes=episodes,
                validator=check,
                overlap=overlap,
                compact=_compact_evidence(evidence),
            )
        )
    write(output / "model-inputs.json", dict(version=1, cases=inputs))
    write(output / "expected-decisions-draft.json", dict(status="unapproved", cases=expected))
    acceptance = (
        apply_acceptance(acceptance_path, spec_path, output, expected) if acceptance_path else None
    )
    budget = dict(
        initial_requests=2 * len(cards),
        conditional_judge_requests_max=2 * len(cards),
        total_requests_max=4 * len(cards),
        retries_included=0,
        orcarouter_requests_max=len(cards),
        beatapi_requests_max=3 * len(cards),
    )
    write(
        output / "record-provenance.json",
        [
            dict(case_id=c["spec"]["id"], retained_rows=c["rows"], native_clips=c["episodes"])
            for c in cards
        ],
    )
    review = render(cards, budget)
    if acceptance:
        review = (
            "# Expected outcomes approved — inference budget pending\n\n"
            "All RQ01–RQ12 expected outcomes were approved by the maintainer on October26.\n"
            "RQ04/RQ05: current manual fallback is accepted; correct automatic resolution is\n"
            "also acceptable with the full feed set and exact recordings. No validator change.\n"
            "RQ07: new-feed expectation confirmed; no feed publication authorized.\n"
            "RQ12: manual control accepted.\n\n"
            "No inference budget, runtime qualification, admission or activation is approved.\n"
            "The original review text below is retained as historical context; its pending\n"
            "questions are superseded by the acceptance record and approved expectation file.\n\n"
            + review
        )
    (output / "REVIEW.md").write_text(review)
    write(
        output / "run-plan-draft.json",
        dict(
            status=(
                "expected_outcomes_approved_awaiting_budget"
                if acceptance
                else "awaiting_expected_outcomes_and_budget_approval"
            ),
            approved=False,
            execution_enabled=False,
            expected_outcomes_approved=bool(acceptance),
            call_budget_approved=False,
            outcome_acceptance_sha256=sha(acceptance_path) if acceptance_path else None,
            budget=budget,
            measurement_transport="direct-single-attempt, runtime-generated payloads",
            automatic_worker_queue_retries_authorized=False,
            runtime_bindings=bindings,
            source_sha256=sources,
            draft_spec_sha256=sha(spec_path),
            model_inputs_sha256=sha(output / "model-inputs.json"),
            expected_draft_sha256=sha(output / "expected-decisions-draft.json"),
            review_sha256=sha(output / "REVIEW.md"),
            constraints=[
                "No provider calls or credentials during preparation.",
                "No independent holdout or passing admission claim.",
                "Judge only actual initial disagreements; judge coverage is not guaranteed.",
                "No qualification for a role with no eligible observed judgments; "
                "no invented holdout/AA eligibility.",
                "Score semantic decisions, validator holds and final cascade "
                "outcomes separately; all-manual is not a passing result.",
                "Rationale wording is not gold. One-off all-observed is equivalent "
                "only for the identical supplied native-clip set.",
                "New-feed slug/title alternatives require semantic human review, "
                "not automatic lexical failure or acceptance.",
                "No retry calls included; no paid fallback or secondary BeatAPI account.",
                "16384 chat output/default reasoning/no temperature/720-second deadline.",
                "Direct single-attempt measurement: no HTTP redirects, SDK/Gateway "
                "retries or fallback.",
                "This qualifies runtime-generated payloads, not Worker queue integration.",
                "65-second provider single-flight spacing; owned provider "
                "pause/drain/restore, exact reservation when registered; never "
                "charge another route.",
                "No production activation, state mutation, publication or deployment.",
            ],
        ),
    )
    print(
        json.dumps(
            dict(
                cases=len(cards),
                actions=Counter(c["spec"]["expected_decision"]["action"] for c in cards),
                budget=budget,
            )
        )
    )


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def render(cards, budget):
    lines = [
        "# Runtime qualification — expected outcomes for review",
        "",
        "**Draft only. No model calls or new approvals have occurred.**",
        "",
        "Review each case below. Reply `RQ01 approve`, or list corrections by case ID.",
        "Actions: union = existing-feed association; single_uid_inclusion = listed recordings",
        "only; new_feed = a distinct body/feed proposal; manual_review = no automatic action.",
        "RQ04/RQ05 require choosing manual fallback versus a separate validator fix.",
        "RQ07 proposes a new feed direction; confirm its identity and scope explicitly.",
        "RQ12 requires deciding whether a manual archive-policy control is fair for this prompt.",
        "Approve expected outcomes separately from the call budget; neither activates production.",
        "",
        f"Budget: {budget['initial_requests']} initial calls plus at most "
        f"{budget['conditional_judge_requests_max']} conditional judge calls = "
        f"**{budget['total_requests_max']} calls maximum**, no retries included.",
        "",
        "This is a selected historical runtime challenge, not an independent production holdout.",
        "Inventory coverage is partial; counts prove observations, not absence elsewhere.",
        "Execution proposal: direct single-attempt measurement of runtime-generated payloads.",
        "No automatic Worker/SDK/Gateway retries; Worker integration is tested separately later.",
        "Full taxonomy/policy and exact job payloads: model-inputs.json. Complete "
        "UID/GUID aliases:",
        "record-provenance.json. No stored meeting records were removed or rewritten.",
        "Mirrored native clips are counted once; original UIDs/GUIDs remain listed below.",
        "Some controls already have selectors today; no selector is removed for this exercise.",
        "The actual initial prompt sees titles/dates/counts/taxonomy/policy, not official agenda",
        "excerpts or these expected decisions. Do not approve a truth requiring hidden evidence.",
        "New-feed metadata is proposed text, not an official title/description replacement.",
        "Validator pass = structurally allowed, not verified truth. All expectations await review.",
        "Separate semantic correctness from exact-consensus/manual cascade outcomes.",
        "",
        "| ID | City / observed label | Proposed action | Validator |",
        "|---|---|---|---|",
    ]
    for card in cards:
        c = card["spec"]
        lines.append(
            f"| [{c['id']}](#{c['id'].lower()}) | {c['city']}: {c['label']} | "
            f"{c['expected_decision']['action']} | {card['validator']['status']} |"
        )
    for card in cards:
        c = card["spec"]
        lines += [
            "",
            f"## {c['id']}",
            "",
            f"**{c['city']} — {c['label']}**",
            "",
            f"Proposed action: **{c['expected_decision']['action']}**.",
            "Target feeds: " + (", ".join(c["expected_decision"]["target_feeds"]) or "none"),
            "",
            c["review_rationale"],
            "",
            f"Observed: {len(card['rows'])} retained rows / {len(card['episodes'])} native clips; "
            f"{len({r['published'][:10] for r in card['episodes']})} distinct dates; "
            f"{card['overlap']} clips overlap the original60 cohort.",
            f"Current validator: **{card['validator']['status']}**.",
        ]
        lines += ["- " + r for r in card["validator"]["reasons"]]
        if c["expected_decision"]["action"] == "new_feed":
            lines += [
                "",
                "Proposed metadata:",
                "```json",
                json.dumps(
                    {k: v for k, v in c["expected_decision"].items() if k.startswith("new_feed_")},
                    indent=2,
                ),
                "```",
            ]
        lines += [
            "",
            "Recordings visible to the runtime (full UID/GUID alias list: record-provenance.json):",
            "",
            "| Episode | Date | Title | UID | Provider GUID / official player |",
            "|---|---|---|---|---|",
        ]
        visible = {
            e["episode_id"] for e in card["compact"]["unexpected_findings"][0]["episode_samples"]
        }
        for i, row in enumerate(card["episodes"]):
            if "e" + str(i) not in visible:
                continue
            guid = row["provider_guid"]
            host = {
                "dallas-tx": "dallastx",
                "addison-tx": "addisontx",
                "denton-tx": "dentontx",
            }.get(c["city"])
            url = (
                guid
                if guid.startswith("https://")
                else f"https://{host}.new.swagit.com/videos/{guid}"
            )
            title = row["title"].replace("|", "\\|")
            lines.append(
                f"| e{i} | {row['published'][:10]} | {title} | {row['uid']} | [{guid}]({url}) |"
            )
        lines += [
            "",
            "Review: **pending**. Confirm action, complete feed set, episode scope and",
            "whether the runtime-visible evidence justifies this expectation.",
            "",
            "<details><summary>Exact runtime finding summary</summary>",
            "",
            "```json",
            json.dumps(card["compact"]["unexpected_findings"][0], indent=2),
            "```",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path)
    args = parser.parse_args()
    build(args.spec, args.output, args.acceptance)
