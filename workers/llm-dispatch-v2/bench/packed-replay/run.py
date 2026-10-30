#!/usr/bin/env python3
"""Replay a private scheduling capture through the real coordinator (see README.md).

Local only: talks to a loopback `wrangler dev` of replay.js. Never commit captures or outputs.
"""

import argparse
import csv
import datetime
import hashlib
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path

TABLES = (
    "jobs",
    "job_models",
    "routes",
    "providers",
    "estimates",
    "dispatch_pause",
    "ingress_purpose",
    "attempt_usage",
    "route_failures",
    "scheduler",
)
TICK_MS = 60_000
# (cumulative share, result fields, authorize-first 429 class)
OUTCOMES = (
    (0.70, {}, None),
    (
        0.78,
        {
            "outcome": "terminal_error",
            "provider_status_code": 400,
            "failure_class": "request_defect",
        },
        None,
    ),
    (
        0.83,
        {"outcome": "terminal_error", "provider_status_code": 429, "failure_class": "own_rpm"},
        "own_rpm",
    ),
    (
        0.88,
        {
            "outcome": "retryable_error",
            "provider_status_code": 503,
            "failure_class": "upstream_capacity",
        },
        None,
    ),
    (
        0.91,
        {
            "outcome": "retryable_error",
            "provider_status_code": 413,
            "failure_class": "route_input_limit",
        },
        None,
    ),
    (
        0.94,
        {
            "outcome": "deferred_late",
            "provider_status_code": None,
            "failure_class": None,
            "actual_start_at": None,
            "actual_end_at": None,
        },
        None,
    ),
    (
        0.96,
        {
            "outcome": "retryable_error",
            "provider_status_code": 410,
            "failure_class": "route_unavailable",
        },
        None,
    ),
    (
        0.98,
        {
            "outcome": "retryable_error",
            "provider_status_code": 402,
            "failure_class": "payment_required",
        },
        None,
    ),
    (
        1.00,
        {
            "outcome": "terminal_error",
            "provider_status_code": 500,
            "failure_class": "output_budget_exhausted",
            "requested_max_tokens": 400,
            "observed_output_tokens": 390,
        },
        None,
    ),
)


def unit(*parts):
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("snapshot", type=Path)
    p.add_argument("schema", type=Path)
    p.add_argument("--variant", choices=("control", "candidate"), required=True)
    p.add_argument("--now", type=int, required=True)
    p.add_argument("--url", default="http://127.0.0.1:18795/")
    p.add_argument("--ticks", type=int, default=80)
    p.add_argument("--prime", type=int, default=2)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--delay-max-s", type=int, default=720)
    p.add_argument("--crash-every", type=int, default=9)
    p.add_argument("--restart-after", type=int)
    p.add_argument("--rollback-after", type=int)
    p.add_argument("--audit-every", type=int, default=10)
    p.add_argument(
        "--seed-failures",
        action="store_true",
        help="Seed four synthetic failure cells (the capture has none) so cutover migrates them",
    )
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    endpoint = urllib.parse.urlsplit(args.url)
    if endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
        p.error("--url must be a local HTTP endpoint")

    schema = sqlite3.connect(":memory:")
    with args.schema.open(newline="") as source:
        for record in csv.DictReader(source):
            if record["sql"].upper().startswith("CREATE TABLE"):
                schema.execute(record["sql"])
    groups = defaultdict(list)
    with args.snapshot.open(newline="") as source:
        for record in csv.DictReader(source):
            groups[record["tbl"]].append(json.loads(record["data"]))
    name = f"replay-{uuid.uuid4()}"

    def post(payload):
        request = urllib.request.Request(
            args.url,
            data=json.dumps({"name": name, **payload}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            sys.exit(f"{payload['action']} failed: {error.code} {error.read().decode()[:500]}")

    for table in TABLES:
        columns = [row[1] for row in schema.execute(f'PRAGMA table_info("{table}")')]
        for start in range(0, len(groups[table]), 200):
            post(
                {
                    "action": "seed",
                    "table": table,
                    "columns": columns,
                    "rows": groups[table][start : start + 200],
                }
            )

    if args.seed_failures:
        columns = [row[1] for row in schema.execute('PRAGMA table_info("route_failures")')]
        rows = []
        for age, kind in ((0, "own_rpm"), (6, "own_tpm"), (8, "unknown_429"), (12, "own_rpm")):
            at = args.now - age * 86_400_000
            day = datetime.datetime.fromtimestamp(at / 1000, datetime.UTC).date().isoformat()
            cell = {
                "utc_day": day,
                "route_id": "gemini_3_1_flash_lite_primary",
                "failure_class": kind,
                "count": age + 1,
                "last_status": 429,
                "last_seen_at": at,
                "last_max_tokens_sent": 100,
                "last_output_tokens": 50,
                "max_output_tokens": 100,
            }
            rows.append([cell.get(column) for column in columns])
        post({"action": "seed", "table": "route_failures", "columns": columns, "rows": rows})

    pending, ticks, audits, events = [], [], [], []
    bundle_ordinal = 0
    packed = False

    def settle(entry, now, record):
        plan = entry["plan"]
        results = []
        authorize = []
        for job in plan["jobs"]:
            roll = unit(args.seed, "outcome", job["id"], job.get("attempts", 0), entry["ordinal"])
            fields, auth = next((f, a) for cut, f, a in OUTCOMES if roll < cut)
            start = entry["claimed_at"] + 100
            result = {
                "job_id": job["id"],
                "attempt_id": f"a-{job['id']}-{entry['ordinal']}",
                "planned_at": job.get("not_before_at"),
                "actual_start_at": start,
                "actual_end_at": max(start, now - 1_000),
                "observed_input_tokens": job.get("input_token_estimate") or 2000,
                "observed_output_tokens": 400,
                "outcome": "success",
                "provider_status_code": 200,
                "result_key": f"results/{job['id']}.json",
                **fields,
            }
            results.append(result)
            if auth:
                authorize.append({"job_id": job["id"], "failure_class": auth})
        meter = post(
            {
                "action": "complete",
                "now": now,
                "plan": plan,
                "results": results,
                "authorize": authorize,
            }
        )
        record["completion"]["reads"] += meter["reads"] + meter["authorization"]["reads"]
        record["completion"]["writes"] += meter["writes"] + meter["authorization"]["writes"]
        record["completion"]["bundles"] += 1

    total = args.prime + args.ticks
    for index in range(total):
        now = args.now + index * TICK_MS
        measured = index >= args.prime
        record = {
            "tick": index - args.prime,
            "now": now,
            "completion": {"reads": 0, "writes": 0, "bundles": 0},
        }
        if args.variant == "candidate" and index == args.prime and not packed:
            record_cutover = post({"action": "cutover", "now": now})
            if not record_cutover["result"]["ok"]:
                sys.exit(f"cutover refused: {record_cutover['result']}")
            events.append({"event": "cutover", "at_tick": index - args.prime, **record_cutover})
            packed = True
        if (
            args.rollback_after is not None
            and measured
            and index - args.prime == args.rollback_after
            and packed
        ):
            rolled = post({"action": "rollback", "now": now})
            if not rolled["result"]["ok"]:
                sys.exit(f"rollback refused: {rolled['result']}")
            events.append({"event": "rollback", "at_tick": index - args.prime, **rolled})
            packed = False
        if args.restart_after is not None and measured and index - args.prime == args.restart_after:
            post({"action": "abort"})
            for attempt in range(5):  # the object restarts on its next call; wait for it to answer
                try:
                    post({"action": "audit"})
                    break
                except SystemExit:
                    if attempt == 4:
                        raise
            events.append({"event": "restart", "at_tick": index - args.prime})
        # Completions due now are attributed to the tick that claimed them.
        still = []
        for entry in pending:
            if entry["due"] <= now:
                settle(entry, now, entry["record"])
            else:
                still.append(entry)
        pending = still
        claim = post({"action": "claim", "now": now})
        plan = claim["plan"]
        record["claim"] = {"reads": claim["reads"], "writes": claim["writes"]}
        record["jobs"] = len(plan.get("jobs") or [])
        record["selection"] = hashlib.sha256(
            json.dumps([[j["id"], j["route_id"]] for j in plan.get("jobs") or []]).encode()
        ).hexdigest()[:16]
        record["reason"] = plan.get("claim_reason")
        record["measured"] = measured
        if record["jobs"]:
            bundle_ordinal += 1
            if not (args.crash_every and bundle_ordinal % args.crash_every == 0):
                delay = 5 + int(unit(args.seed, "delay", bundle_ordinal) * (args.delay_max_s - 5))
                pending.append(
                    {
                        "due": now + delay * 1000,
                        "plan": plan,
                        "claimed_at": now,
                        "ordinal": bundle_ordinal,
                        "record": record,
                    }
                )
        ticks.append(record)
        if measured and (index - args.prime + 1) % args.audit_every == 0:
            audits.append({"tick": index - args.prime, **post({"action": "audit"})})
    # Completions still in flight at the end belong to their originating ticks.
    end = args.now + total * TICK_MS
    for entry in sorted(pending, key=lambda e: e["due"]):
        settle(entry, max(end, entry["due"]), entry["record"])
    final = post({"action": "audit"})
    final_stats = post({"action": "stats", "now": end + TICK_MS})
    final["recovery"] = final_stats.get("recovery")
    final["authority_stats"] = final_stats.get("authority")
    measured = [t for t in ticks if t["measured"]]

    def cost(tick):
        return (
            tick["claim"]["reads"] + tick["completion"]["reads"],
            tick["claim"]["writes"] + tick["completion"]["writes"],
        )

    reads = [cost(t)[0] for t in measured]
    writes = [cost(t)[1] for t in measured]
    summary = {
        "variant": args.variant,
        "ticks": len(measured),
        "jobs_claimed": sum(t["jobs"] for t in measured),
        "reads_mean": sum(reads) / len(reads),
        "reads_max": max(reads),
        "writes_mean": sum(writes) / len(writes),
        "writes_max": max(writes),
        "writes_total": sum(writes),
        "reads_total": sum(reads),
        "ticks_over_40_writes": sum(1 for w in writes if w > 40),
    }
    args.out.write_text(
        json.dumps(
            {
                "summary": summary,
                "events": events,
                "ticks": ticks,
                "audits": audits,
                "final": final,
            },
            indent=1,
        )
    )
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
