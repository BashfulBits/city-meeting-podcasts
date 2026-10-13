"""Pure recovery planning for the approved evaluation; no credentials or provider calls."""

import json
import math
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MODELS = ("flashlite", "kimi", "glm_full", "glm_flash", "sol", "deepseek", "gemini37", "gemini38")
PHYSICAL = (*MODELS, "gemini37_secondary", "gemini38_secondary")
COOLDOWN_SECONDS = 1800
ATTEMPTS_PER_DAY = 3
RETRIES_PER_RUN = 3


def logical_candidate(candidate):
    return candidate.removesuffix("_secondary")


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("evaluation timestamps must include a timezone")
    return result.astimezone(UTC)


def next_reset(now, timezone):
    local = now.astimezone(ZoneInfo(timezone or "UTC"))
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=local.tzinfo).astimezone(UTC)


def sanitize(value):
    text = str(value)
    for key, secret in os.environ.items():
        if any(part in key for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and len(secret) > 7:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(key=|bearer\s+)[^\s&]+", r"\1[REDACTED]", text)[:2000]


def provider_error(exc, now):
    """Keep actual failure details, never fabricate 429 for exceptions without a status."""
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    headers = getattr(exc, "headers", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    retry_after = None
    if "retry-after" in headers:
        try:
            retry_after = float(headers["retry-after"])
            if not math.isfinite(retry_after) or retry_after < 0:
                retry_after = None
        except ValueError:
            try:
                retry_after = max(
                    0, (parsedate_to_datetime(headers["retry-after"]) - now).total_seconds()
                )
            except (ValueError, TypeError):
                pass
    name = type(exc).__name__
    if "Timeout" in name or status == 408:
        category = "timeout"
    elif status == 429:
        category = "rate_limit"
    elif status is not None and status >= 500:
        category = "server_error"
    elif status in {401, 403, 404}:
        category = "access_error"
    elif isinstance(exc, ValueError):
        category = "invalid_answer"
    else:
        category = "backend_error"
    return {
        "http_status": status,
        "category": category,
        "message": sanitize(exc),
        "exception_class": name,
        "retry_after_seconds": retry_after,
        "gateway_error": sanitize(headers.get("cf-aig-error", "")),
    }


def finished_at(row):
    if row.get("finished_at"):
        return timestamp(row["finished_at"])
    # Legacy observations timestamp the start, not the end of their provider attempt.
    return timestamp(row["observed_at"]) + timedelta(seconds=row.get("end_to_end_seconds", 0))


def failure_details(row):
    if row.get("provider_error"):
        return row["provider_error"]
    # Older files remain immutable. Infer only what their retained reason/class proves.
    name = row.get("error_class", "")
    if "Timeout" in name:
        category = "timeout"
    elif "ServerError" in name or "Unavailable" in name:
        category = "server_error"
    elif name == "ValueError":
        category = "invalid_answer"
    else:
        category = "backend_error"
    return {"category": category, "retry_after_seconds": None}


def cooldown_until(row):
    details = failure_details(row)
    if details["category"] == "access_error":
        return None
    delay = max(COOLDOWN_SECONDS, details.get("retry_after_seconds") or 0)
    return finished_at(row) + timedelta(seconds=delay)


def read_history(directory, manifest_hash, case_ids):
    rows = []
    completed_inputs = {}
    for path in sorted(Path(directory).glob("*/*.json")):
        row = json.loads(path.read_text())
        if row.get("manifest_hash") != manifest_hash:
            raise ValueError(f"manifest mismatch in {path}")
        if row.get("case_id") not in case_ids or row.get("candidate") not in (*PHYSICAL, "astra"):
            raise ValueError(f"unknown evaluation pair in {path}")
        if row.get("status") not in {"completed", "failed", "deferred"}:
            raise ValueError(f"unknown observation status in {path}")
        attempts = row.get("provider_attempts", 0)
        if type(attempts) is not int or not 0 <= attempts <= 1:
            raise ValueError(f"invalid provider attempt count in {path}")
        timestamp(row["observed_at"])
        if row["status"] == "completed":
            pair = (logical_candidate(row["candidate"]), row["case_id"])
            context = tuple(
                row.get(k) for k in ("prompt_hash", "schema_hash", "case_input_hash", "mode")
            )
            if pair in completed_inputs and completed_inputs[pair] != context:
                raise ValueError(f"conflicting completed inputs in {path}")
            completed_inputs[pair] = context
        rows.append(row)
    resolved = {row["attempt_id"] for row in rows if row.get("attempt_id")}
    for path in sorted((Path(directory).parent / "intents").glob("*.json")):
        intent = json.loads(path.read_text())
        if intent.get("attempt_id") in resolved:
            continue
        if (
            intent.get("manifest_hash") != manifest_hash
            or intent.get("case_id") not in case_ids
            or intent.get("candidate") not in PHYSICAL
            or not intent.get("attempt_id")
            or intent.get("provider_attempts") != 1
        ):
            raise ValueError(f"invalid interrupted attempt intent in {path}")
        timestamp(intent["observed_at"])
        rows.append(intent | {"status": "deferred", "unknown_outcome": True})
    return rows


def candidate_plan(rows, candidate, case_ids, now, started_at):
    family = logical_candidate(candidate)
    related = [r for r in rows if logical_candidate(r["candidate"]) == family]
    physical = [r for r in related if r["candidate"] == candidate]
    selected = set(case_ids)
    done = {r["case_id"] for r in related if r["status"] == "completed"} & selected
    failed = ({r["case_id"] for r in related if r["status"] == "failed"} & selected) - done
    called = {r["case_id"] for r in related if r.get("provider_attempts", 0)} & selected
    unknown = {r["case_id"] for r in related if r.get("unknown_outcome")} - done
    plan = {
        "candidate": candidate,
        "answered": len(done),
        "failed_pending": len(failed),
        "never_called": len(set(case_ids) - called),
        "eligible_case_ids": [],
        "retry_exhausted": 0,
        "unknown_outcomes": len(unknown),
        "state": "complete" if len(done) == len(case_ids) else "ready",
        "next_eligible_at": None,
    }
    if plan["state"] == "complete":
        return plan
    failures = [r for r in physical if r["status"] == "failed"]
    if failures:
        latest = max(failures, key=finished_at)
        until = cooldown_until(latest)
        if until is None:
            return plan | {"state": "access_error"}
        if until > now:
            return plan | {"state": "cooldown", "next_eligible_at": until.isoformat()}
    today = Counter()
    new_attempts = Counter()
    ever_called = {r["case_id"] for r in physical if r.get("provider_attempts", 0)}
    for row in physical:
        if timestamp(row["observed_at"]).date() == now.astimezone(UTC).date():
            today[row["case_id"]] += row.get("provider_attempts", 0)
        if timestamp(row["observed_at"]) >= started_at:
            new_attempts[row["case_id"]] += row.get("provider_attempts", 0)
    for case_id in case_ids:
        if case_id in done:
            continue
        if case_id in unknown:
            continue
        if case_id in ever_called and new_attempts[case_id] >= RETRIES_PER_RUN:
            plan["retry_exhausted"] += 1
        elif today[case_id] >= ATTEMPTS_PER_DAY:
            plan["next_eligible_at"] = next_reset(now, "UTC").isoformat()
        else:
            plan["eligible_case_ids"].append(case_id)
    plan["eligible_case_ids"].sort(key=lambda c: c in ever_called)
    if not plan["eligible_case_ids"]:
        if plan["next_eligible_at"]:
            plan["state"] = "daily_attempt_limit"
        else:
            plan["state"] = "unknown_outcome" if unknown else "retry_exhausted"
    return plan


def atomic_status(path, state):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w") as output:
            json.dump(state, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
