"""LLM endpoint rate-limit characterization and failure-class probe harness.

This tool intentionally uses a fixed harmless prompt and never opens storage, records,
or the LLM budget ledger. It measures enforced RPM, burst capacity, input ceilings,
and recovery timing across catalog routes without consuming production state.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from citypods.compute.llm_failure_class import classify_provider_failure

FIXED_PROMPT = "Ping"
HEADER_REGEX = re.compile(
    r"^(x-ratelimit-|ratelimit-|retry-after|cf-aig-|cf-ray|x-request-id)",
    re.IGNORECASE,
)
ROUTES_FILE = Path(__file__).resolve().parent / "compute" / "llm_routes.json"


def load_route_catalog() -> list[dict[str, Any]]:
    """Load routes directly from compiled llm_routes.json."""
    if not ROUTES_FILE.exists():
        raise FileNotFoundError(f"Missing compiled routes at {ROUTES_FILE}")
    data = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
    return list(data.get("routes", []))


class ProbeBudgetExceeded(Exception):
    """Raised when total request or wall-clock budget is exceeded."""


class RouteBudgetExceeded(Exception):
    """Raised when request budget for a single route is exceeded."""


class RateProbeRunner:
    """Manages probe execution, safety limits, and live provider calls."""

    def __init__(
        self,
        *,
        apply: bool = False,
        max_requests_per_route: int = 40,
        max_requests_total: int = 400,
        max_wall_seconds: int = 900,
        session: requests.Session | None = None,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        self.apply = apply
        self.max_requests_per_route = max_requests_per_route
        self.max_requests_total = max_requests_total
        self.max_wall_seconds = max_wall_seconds
        self.session = session or requests.Session()
        self.request_timeout_seconds = request_timeout_seconds
        self.total_requests = 0
        self.route_request_counts: dict[str, int] = {}
        self.start_time = time.monotonic()

    def check_budgets(self, route_id: str) -> None:
        """Check wall-clock, total-request, and per-route request ceilings."""
        elapsed = time.monotonic() - self.start_time
        if elapsed >= self.max_wall_seconds:
            raise ProbeBudgetExceeded(
                f"Max wall time of {self.max_wall_seconds}s exceeded ({elapsed:.1f}s elapsed)"
            )
        if self.total_requests >= self.max_requests_total:
            raise ProbeBudgetExceeded(f"Max total requests of {self.max_requests_total} reached")
        route_count = self.route_request_counts.get(route_id, 0)
        if route_count >= self.max_requests_per_route:
            raise RouteBudgetExceeded(
                f"Max requests per route ({self.max_requests_per_route}) reached for {route_id}"
            )

    def send_request(
        self,
        route: dict[str, Any],
        prompt: str,
        *,
        max_tokens: int = 1,
    ) -> dict[str, Any]:
        """Send a single live request to a route, enforcing safety budgets."""
        route_id = route.get("route_id", "unknown")
        self.check_budgets(route_id)

        if not self.apply:
            # Dry-run: zero live calls
            return {
                "dry_run": True,
                "status": None,
                "headers": {},
                "body": None,
            }

        api_key_env = route.get("api_key_env")
        api_key = os.environ.get(api_key_env or "")
        if not api_key:
            return {
                "error": "missing_api_key",
                "status": None,
                "headers": {},
                "body": None,
            }

        url = direct_chat_url(route)

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": route.get("upstream_model"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        }

        self.total_requests += 1
        self.route_request_counts[route_id] = self.route_request_counts.get(route_id, 0) + 1

        try:
            resp = self.session.post(
                url, headers=headers, json=payload, timeout=self.request_timeout_seconds
            )
            status = resp.status_code
            captured_headers = {
                k.lower(): v for k, v in resp.headers.items() if HEADER_REGEX.search(k)
            }
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return {
                "status": status,
                "headers": captured_headers,
                "body": body,
            }
        except requests.RequestException as exc:
            return {
                "exception": type(exc).__name__,
                "error": str(exc),
                "status": None,
                "headers": {},
                "body": None,
            }


def run_phase_0(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 0: Reachability & header inventory (1 request/route)."""
    resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
    if resp.get("dry_run"):
        return {"phase": "0", "dry_run": True}

    status = resp.get("status")
    headers = resp.get("headers", {})
    body = resp.get("body")

    error_info: dict[str, Any] = {}
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            error_info = {
                "type": err.get("type"),
                "code": err.get("code"),
                "status": err.get("status"),
                "message": str(err.get("message") or "")[:300],
            }

    cls = None
    if status is not None:
        cls = classify_provider_failure(
            status=status,
            body=body,
            headers=headers,
            route=route,
        )

    return {
        "phase": "0",
        "status": status,
        "headers": headers,
        "error": error_info,
        "classification": (
            {
                "failure_class": cls.failure_class,
                "rule_id": cls.rule_id,
                "scope": cls.scope,
            }
            if cls
            else None
        ),
    }


def run_phase_1(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 1: Declared-vs-enforced RPM."""
    rpm = float(route.get("rpm") or 10)
    target_count = int(math.ceil(min(rpm, 20.0))) + 2
    spacing = 60.0 / rpm if rpm > 0 else 6.0

    first_429_index = None
    first_429_class = None
    successful_requests = 0

    for i in range(1, target_count + 1):
        if i > 1 and runner.apply:
            time.sleep(spacing)
        resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
        if resp.get("dry_run"):
            return {"phase": "1", "dry_run": True, "target_requests": target_count}
        if resp.get("status") == 429:
            first_429_index = i
            cls = classify_provider_failure(
                status=429,
                body=resp.get("body"),
                headers=resp.get("headers"),
                route=route,
            )
            first_429_class = cls.failure_class
            break
        if resp.get("status") == 200:
            successful_requests += 1
        else:
            break

    return {
        "phase": "1",
        "target_requests": target_count,
        "first_429_index": first_429_index,
        "first_429_class": first_429_class,
        "enforced_rpm_at_least": successful_requests if first_429_index is None else None,
    }


def run_phase_1b(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 1b: Burst capacity."""
    rpm = float(route.get("rpm") or 10)
    burst_limit = int(math.ceil(min(2 * rpm + 5, 40.0)))
    observed_burst = 0

    for _ in range(burst_limit):
        resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
        if resp.get("dry_run"):
            return {"phase": "1b", "dry_run": True, "max_burst_probes": burst_limit}
        if is_usable_completion(resp):
            observed_burst += 1
        else:
            break

    return {
        "phase": "1b",
        "max_burst_probes": burst_limit,
        "observed_burst": observed_burst,
    }


def is_usable_completion(resp: Mapping[str, Any]) -> bool:
    """A 200 is not automatically an answer.

    Airforce returns HTTP 200 with no `choices` and an error object whose own code says 503
    (confirmed live 2026-09-09). Counting that as a success would mark a route that is serving
    nothing as "viable" -- in a run whose entire purpose is separating a dead route from a busy
    one, which is the one distinction it must not get wrong.
    """
    if resp.get("status") != 200:
        return False
    body = resp.get("body")
    if not isinstance(body, dict):
        return False
    if body.get("error"):
        return False
    choices = body.get("choices")
    return isinstance(choices, list) and len(choices) > 0


def direct_chat_url(route: Mapping[str, Any]) -> str:
    """The provider's real direct chat-completions URL for a route.

    `api_base` + `chat_path` cannot simply be concatenated. Those two fields are authored for
    Cloudflare AI Gateway's custom-provider path rewrite (see ARCHITECTURE on the undocumented
    "last Base URL segment becomes /v1" behaviour), which requires some providers to repeat a
    segment that is ALREADY part of `api_base`. Airforce is the live example:

        api_base  https://api.airforce/v1
        chat_path /v1/chat/completions
        naive     https://api.airforce/v1/v1/chat/completions  -> 404 not_found
        correct   https://api.airforce/v1/chat/completions     -> reaches the provider

    So a duplicated leading segment is collapsed. Production is unaffected either way -- it calls
    these providers through the Gateway, which applies its own rewrite -- but the probe goes
    direct, and with the naive join it silently 404'd every airforce request. That made a
    reachable route look permanently dead in an endurance run whose entire purpose is telling
    "dead" apart from "busy".
    """
    api_base = str(route.get("api_base", "")).rstrip("/")
    chat_path = str(route.get("chat_path") or "/v1/chat/completions")
    if not chat_path.startswith("/"):
        chat_path = "/" + chat_path
    first_segment = chat_path.split("/")[1] if "/" in chat_path[1:] else ""
    if first_segment and api_base.endswith("/" + first_segment):
        chat_path = chat_path[len(first_segment) + 1 :]
    return f"{api_base}{chat_path}"


def provider_reported_token_limit(resp: dict[str, Any]) -> int | None:
    """The provider's OWN per-minute token budget, read from the response it just sent us.

    Two sources, header first because it is present on successes too:
      * ``x-ratelimit-limit-tokens: 8000``
      * the error body, e.g. "...on input tokens per minute (ITPM): Limit 7000, Requested 17789..."

    This is the only trustworthy bound for a ceiling search. Our own configured ``tpm`` is an
    input to pacing, not a measurement -- it can be stale or conservative -- and the model's
    advertised context window is irrelevant when a per-minute token budget sits far below it
    (Groq advertises a 131,072-token context on an account whose ITPM is 7,000, so no single
    request may exceed ~7,000 no matter what the context window says).
    """
    headers = resp.get("headers") or {}
    for key in ("x-ratelimit-limit-tokens", "x-ratelimit-limit-input-tokens"):
        raw = headers.get(key)
        if raw is not None:
            try:
                parsed = int(float(str(raw).strip()))
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                return parsed

    body = resp.get("body")
    message = ""
    if isinstance(body, dict):
        # `error` is a dict for most providers but a bare string for some (and absent for others),
        # so every access has to be shape-checked -- a live endurance run died here on the first
        # provider that returned {"error": "..."}.
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("detail") or "")
        elif isinstance(err, str):
            message = err
        if not message:
            message = str(body.get("message") or body.get("detail") or "")
    elif isinstance(body, str):
        message = body
    match = re.search(r"limit[:\s]+([0-9][0-9,_]*)", message, re.IGNORECASE)
    if match:
        try:
            parsed = int(match.group(1).replace(",", "").replace("_", ""))
        except ValueError:
            return None
        if parsed > 0:
            return parsed
    return None


def _throttle_class(route: dict[str, Any], resp: dict[str, Any]) -> str | None:
    """The failure class of a non-200 response, or None for a success.

    Uses the same shared signature table the Worker enforces with, so the probe and production
    can never disagree about what a given provider body means.
    """
    if resp.get("status") == 200:
        return None
    return classify_provider_failure(
        status=resp.get("status") or 0,
        body=resp.get("body"),
        headers=resp.get("headers") or {},
        route=route,
    ).failure_class


# Classes that say nothing whatsoever about how large the request was. Narrowing a ceiling search
# on one of these is how the 2026-09-09 run recorded five routes at the search floor (see
# run_phase_2's docstring).
_NON_SIZE_CLASSES = frozenset(
    {
        "upstream_capacity",
        "gateway_limit",
        "server_error",
        "own_rpm",
        "own_rpd",
        "own_tpm",
        "payment_required",
        "unknown_429",
    }
)


def run_phase_2(
    runner: RateProbeRunner,
    route: dict[str, Any],
    *,
    max_probes: int = 8,
    throttle_retries: int = 0,
    throttle_wait_seconds: float = 60.0,
    provider_reported_tpm: int | None = None,
    confirm_rounds: int = 2,
    confirm_wait_seconds: float = 25.0,
) -> dict[str, Any]:
    """Phase 2: enforced input ceiling by binary search.

    Two bugs in the original made this actively dangerous, because its output was auto-promoted
    into an enforced `hard_input_ceiling` that permanently removes a route from the ranking:

    1. It seeded `observed_ceiling` to the search FLOOR (1000) and only ever raised it on a 200,
       so "nothing was ever accepted" and "the ceiling is exactly 1000" produced identical output.
    2. It narrowed the search on 429, treating a rate limit as a size signal -- the precise
       confusion Initiative 20 exists to remove. A route that was merely upstream-saturated got
       recorded as having a 1,000-token context window.

    Both are fixed here: the ceiling starts as None and is only ever set to a size that actually
    returned 200, and only a *size* rejection narrows the search. A throttle is retried at the
    same size (up to ``throttle_retries``) and otherwise abandons the search as INCONCLUSIVE
    rather than inventing a number. A None result must never be written to config.
    """
    context_limit = route.get("input_context_limit") or 32000
    low = 1000
    # Upper bound: the model's advertised context window, but never above the provider's own
    # per-minute token budget once it tells us one -- a request can never exceed the budget it
    # would have to spend. `provider_tpm` is refined from every response below, so a route that
    # only reveals its budget in an error still gets a correctly-bounded search. The 250,000 cap
    # is a probe-cost guard, not a claim about any provider.
    provider_tpm = provider_reported_tpm
    high = min(context_limit, 250000)
    if provider_tpm:
        high = min(high, provider_tpm)
    observed_ceiling: int | None = None
    largest_rejected: int | None = None
    inconclusive_reason: str | None = None

    filler_sentence = "The quick brown fox jumps over the lazy dog. "
    chars_per_token = 4

    def _probe(size: int) -> dict[str, Any]:
        char_count = size * chars_per_token
        multiplier = max(1, char_count // len(filler_sentence))
        return runner.send_request(route, (filler_sentence * multiplier)[:char_count], max_tokens=1)

    probes = 0
    while probes < max_probes and low < high - 1000:
        mid = (low + high) // 2
        resp = _probe(mid)
        if resp.get("dry_run"):
            return {"phase": "2", "dry_run": True, "context_limit": context_limit}
        probes += 1

        reported = provider_reported_token_limit(resp)
        if reported and (provider_tpm is None or reported < provider_tpm):
            provider_tpm = reported
            if provider_tpm < high:
                high = max(low + 1, provider_tpm)

        cls = _throttle_class(route, resp)
        attempts_left = throttle_retries
        while cls in _NON_SIZE_CLASSES and attempts_left > 0:
            # A throttle says nothing about size. Wait it out and re-probe the SAME size.
            time.sleep(throttle_wait_seconds)
            resp = _probe(mid)
            if resp.get("dry_run"):
                return {"phase": "2", "dry_run": True, "context_limit": context_limit}
            probes += 1
            attempts_left -= 1
            cls = _throttle_class(route, resp)

        if is_usable_completion(resp):
            observed_ceiling = mid
            low = mid
        elif cls in _NON_SIZE_CLASSES:
            inconclusive_reason = f"throttled ({cls}) and never cleared at size {mid}"
            break
        elif resp.get("status") in (400, 413):
            largest_rejected = mid if largest_rejected is None else min(largest_rejected, mid)
            high = mid
        else:
            inconclusive_reason = f"unexpected status {resp.get('status')} at size {mid}"
            break

    # Boundary confirmation. A rejection at the boundary may not be a property of the ROUTE at
    # all: this account's Gemini/Gemma, NVIDIA and Airforce routes carry live production traffic
    # from the v2 Worker every few minutes, so a probe can be refused for budget another process
    # just spent. Accepting that first refusal as the ceiling understates it -- the same mistake
    # in a subtler form as reading a 429 as a size limit. So re-test the smallest rejected size a
    # few times, spaced; if it ever succeeds, the boundary was contention and the search resumes
    # upward from there rather than freezing at an artificially low number.
    contention_detected = False
    rounds = confirm_rounds
    while rounds > 0 and largest_rejected is not None and probes < max_probes + confirm_rounds * 2:
        rounds -= 1
        if confirm_wait_seconds:
            time.sleep(confirm_wait_seconds)
        resp = _probe(largest_rejected)
        if resp.get("dry_run"):
            break
        probes += 1
        if is_usable_completion(resp):
            # The "ceiling" moved once contention cleared -- it was never a ceiling.
            contention_detected = True
            observed_ceiling = largest_rejected
            low = largest_rejected
            high = min(context_limit, 250000)
            if provider_tpm:
                high = min(high, provider_tpm)
            largest_rejected = None
            while probes < max_probes + confirm_rounds * 2 and low < high - 1000:
                mid = (low + high) // 2
                resp = _probe(mid)
                if resp.get("dry_run"):
                    break
                probes += 1
                if is_usable_completion(resp):
                    observed_ceiling = mid
                    low = mid
                elif _throttle_class(route, resp) in _NON_SIZE_CLASSES:
                    break
                elif resp.get("status") in (400, 413):
                    largest_rejected = (
                        mid if largest_rejected is None else min(largest_rejected, mid)
                    )
                    high = mid
                else:
                    break

    return {
        "phase": "2",
        # None means "not established". Never fall back to the search floor: an unverified number
        # here becomes a permanent route block downstream.
        "observed_input_ceiling": observed_ceiling,
        "smallest_rejected_size": largest_rejected,
        "advertised_context_limit": context_limit,
        "probes_used": probes,
        "inconclusive_reason": inconclusive_reason,
        "conclusive": observed_ceiling is not None,
        # The provider's own stated budget, when it gave one. Where this sits below our configured
        # `tpm`, config is wrong and should be corrected; where it sits far above, our `tpm` is
        # merely conservative and a larger ceiling is available.
        # True when a size we had recorded as rejected later succeeded unchanged -- proof the
        # first refusal was contention for a shared account budget, not a property of the route.
        # Any ceiling from such a run is a LOWER BOUND and must not be promoted to enforcement.
        "contention_detected": contention_detected,
        "provider_reported_tpm": provider_tpm,
        "configured_tpm": route.get("tpm"),
        "search_upper_bound": high,
    }


def run_phase_3(
    runner: RateProbeRunner,
    route: dict[str, Any],
    last_429: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 3: Recovery timing after 429."""
    intervals = [5, 15, 30, 60, 120, 300]
    observed_recovery = None

    for delay in intervals:
        if runner.apply:
            time.sleep(delay)
        resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
        if resp.get("dry_run"):
            return {"phase": "3", "dry_run": True}
        if resp.get("status") == 200:
            observed_recovery = delay
            break

    advertised_retry_after = None
    retry_after_trustworthy = None
    if last_429:
        headers = last_429.get("headers") or {}
        ra_val = headers.get("retry-after")
        if ra_val is not None:
            try:
                advertised_retry_after = float(ra_val)
                if observed_recovery is not None and advertised_retry_after > 0:
                    ratio = advertised_retry_after / float(observed_recovery)
                    retry_after_trustworthy = 0.5 <= ratio <= 1.5
            except (ValueError, TypeError):
                pass

    return {
        "phase": "3",
        "observed_recovery_seconds": observed_recovery,
        "advertised_retry_after_seconds": advertised_retry_after,
        "retry_after_trustworthy": retry_after_trustworthy,
    }


def run_phase_4(
    p0_observation: dict[str, Any],
) -> dict[str, Any] | None:
    """Phase 4: Upstream-vs-own labeling against cold first request from Phase 0."""
    if p0_observation.get("status") != 429:
        return None

    cls_info = p0_observation.get("classification") or {}
    classifier_class = cls_info.get("failure_class")
    ground_truth = "upstream_capacity"
    agreed = classifier_class == ground_truth

    return {
        "phase": "4",
        "ground_truth": ground_truth,
        "classified_as": classifier_class,
        "rule_id": cls_info.get("rule_id"),
        "agreed": agreed,
    }


def run_endurance(
    routes: list[dict[str, Any]],
    *,
    apply: bool,
    hours: float = 3.0,
    interval_seconds: float = 60.0,
    session: requests.Session | None = None,
    log=print,
) -> dict[str, Any]:
    """Persist against every route for up to ``hours``, never giving up on a throttle.

    The question this answers is deliberately different from Phase 0/1's. Those ask "what happens
    when we call this route once?" -- which conflates a route that is permanently broken with one
    that is merely busy right now. This asks "does this route EVER accept a job if we keep asking
    politely for three hours?", which is the question production actually cares about, because
    production can keep retrying.

    Policy, per the 2026-09-09 review:
      * A throttle (429, a rate-limit-shaped 413, 5xx, gateway limit) is never terminal here. The
        route stays in the rotation and is retried on the next tick.
      * Every route is polled at least once per ``interval_seconds`` until it succeeds once.
      * A route that never returns a single 200 in the whole window is reported ``viable: False``
        -- it is not useful to production and should be disabled.
      * A route that succeeds even once is ``viable: True``; production should keep retrying it.
        Its true input ceiling is then measured with the throttle-tolerant Phase 2 search, which
        retries through a rate limit instead of mistaking it for a size limit.
    """
    deadline = time.monotonic() + hours * 3600.0
    runner = RateProbeRunner(
        apply=apply,
        # Endurance runs deliberately outside the one-shot caps: the whole point is sustained
        # polling. The wall clock is the real budget, so make the count ceilings non-binding.
        max_requests_per_route=10**9,
        max_requests_total=10**9,
        max_wall_seconds=int(hours * 3600) + 3600,
        session=session,
        # Endurance asks whether a route can EVER serve, not whether it is fast. NVIDIA has been
        # observed taking >45s for a one-token request; at the 15s default those routes record as
        # transport failures and would be recommended for disable purely for being slow.
        request_timeout_seconds=90.0,
    )
    state: dict[str, dict[str, Any]] = {
        r["route_id"]: {
            "route_id": r["route_id"],
            "provider": r.get("provider"),
            "upstream_model": r.get("upstream_model"),
            "attempts": 0,
            "successes": 0,
            "first_success_seconds": None,
            "failure_classes": {},
            "last_status": None,
            "provider_reported_tpm": None,
            "skipped": None,
        }
        for r in routes
    }
    started = time.monotonic()
    pending = {r["route_id"]: r for r in routes}
    next_due = {rid: started for rid in pending}

    # Drop routes with no credential up front rather than burning the window on them.
    for rid, route in list(pending.items()):
        if apply and not os.environ.get(route.get("api_key_env") or ""):
            state[rid]["skipped"] = f"missing {route.get('api_key_env')}"
            pending.pop(rid)
            next_due.pop(rid, None)

    log(
        f"endurance: {len(pending)} routes, {hours}h window, "
        f">=1 request/{interval_seconds:.0f}s each"
    )
    while pending and time.monotonic() < deadline:
        now = time.monotonic()
        due = [rid for rid, at in next_due.items() if at <= now and rid in pending]
        if not due:
            time.sleep(min(1.0, max(0.0, min(next_due.values()) - now)))
            continue
        for rid in due:
            if time.monotonic() >= deadline:
                break
            route = pending[rid]
            resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
            if resp.get("dry_run"):
                return {"mode": "endurance", "dry_run": True, "routes": len(routes)}
            st = state[rid]
            st["attempts"] += 1
            st["last_status"] = resp.get("status")
            reported = provider_reported_token_limit(resp)
            if reported and (
                st.get("provider_reported_tpm") is None or reported < st["provider_reported_tpm"]
            ):
                st["provider_reported_tpm"] = reported
            next_due[rid] = time.monotonic() + interval_seconds
            if is_usable_completion(resp):
                st["successes"] += 1
                st["first_success_seconds"] = round(time.monotonic() - started, 1)
                log(
                    f"  [{st['first_success_seconds']:>7.1f}s] {rid} SUCCEEDED after "
                    f"{st['attempts']} attempt(s)"
                )
                pending.pop(rid, None)
                next_due.pop(rid, None)
            else:
                cls = _throttle_class(route, resp) or "unknown"
                st["failure_classes"][cls] = st["failure_classes"].get(cls, 0) + 1

    elapsed = round(time.monotonic() - started, 1)
    for rid in pending:
        state[rid]["exhausted_window"] = True

    # Measure the true ceiling only for routes that proved they can be served at all.
    viable = [rid for rid, s in state.items() if s["successes"] > 0]
    ceilings: dict[str, Any] = {}
    if apply:
        for rid in viable:
            route = next(r for r in routes if r["route_id"] == rid)
            ceilings[rid] = run_phase_2(
                runner,
                route,
                max_probes=8,
                throttle_retries=3,
                throttle_wait_seconds=20.0,
                provider_reported_tpm=state[rid].get("provider_reported_tpm"),
            )

    results = []
    for rid, s in state.items():
        s = dict(s)
        s["viable"] = s["successes"] > 0
        s["recommend_disable"] = s["successes"] == 0 and s["skipped"] is None and s["attempts"] > 0
        s["ceiling"] = ceilings.get(rid)
        results.append(s)
    results.sort(key=lambda s: (not s["viable"], s["route_id"]))

    return {
        "mode": "endurance",
        "window_hours": hours,
        "interval_seconds": interval_seconds,
        "elapsed_seconds": elapsed,
        "total_requests": runner.total_requests,
        "viable_count": len(viable),
        "unreliable_count": sum(1 for s in results if s["recommend_disable"]),
        "routes": results,
    }


def run_probes(
    routes: list[dict[str, Any]],
    phases: list[str],
    runner: RateProbeRunner,
) -> dict[str, Any]:
    """Run specified phases across target routes and collect report data."""
    results: list[dict[str, Any]] = []

    for route in routes:
        route_id = route.get("route_id", "unknown")
        api_key_env = route.get("api_key_env")
        if runner.apply and (not api_key_env or not os.environ.get(api_key_env)):
            results.append(
                {
                    "route_id": route_id,
                    "provider": route.get("provider"),
                    "status": "skipped",
                    "reason": f"Missing {api_key_env} in environment",
                }
            )
            continue

        route_record: dict[str, Any] = {
            "route_id": route_id,
            "provider": route.get("provider"),
            "model": route.get("model"),
            "observations": {},
        }

        last_429 = None
        try:
            p0_res = None
            if "0" in phases:
                p0_res = run_phase_0(runner, route)
                route_record["observations"]["phase_0"] = p0_res
                if p0_res.get("status") == 429:
                    last_429 = p0_res

            if "1" in phases:
                p1_res = run_phase_1(runner, route)
                route_record["observations"]["phase_1"] = p1_res
                if p1_res.get("first_429_index") is not None:
                    last_429 = p1_res

            if "1b" in phases:
                route_record["observations"]["phase_1b"] = run_phase_1b(runner, route)

            if "2" in phases:
                route_record["observations"]["phase_2"] = run_phase_2(runner, route)

            if "3" in phases:
                route_record["observations"]["phase_3"] = run_phase_3(
                    runner, route, last_429=last_429
                )

            if "4" in phases:
                if p0_res:
                    p4_res = run_phase_4(p0_res)
                    if p4_res:
                        route_record["observations"]["phase_4"] = p4_res

            results.append(route_record)
        except RouteBudgetExceeded as exc:
            route_record["budget_interrupted"] = str(exc)
            results.append(route_record)
            continue
        except ProbeBudgetExceeded as exc:
            route_record["budget_interrupted"] = str(exc)
            results.append(route_record)
            break

    # Calculate Phase 4 agreement rate if applicable
    p4_agreements = 0
    p4_total = 0
    for r in results:
        obs = r.get("observations", {})
        p4 = obs.get("phase_4")
        if p4 and isinstance(p4, dict):
            p4_total += 1
            if p4.get("agreed"):
                p4_agreements += 1

    summary: dict[str, Any] = {
        "apply": runner.apply,
        "total_requests": runner.total_requests,
        "elapsed_seconds": round(time.monotonic() - runner.start_time, 2),
        "routes_probed": len(results),
        "phase_4_agreement_rate": (round(p4_agreements / p4_total, 3) if p4_total > 0 else None),
        "phase_4_total_evaluations": p4_total,
    }

    return {"summary": summary, "routes": results}


def emit_github_step_summary(report: dict[str, Any]) -> None:
    """Render markdown summary into GITHUB_STEP_SUMMARY if present."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    summary = report.get("summary", {})
    routes = report.get("routes", [])

    lines = [
        "## LLM Rate-Limit Probe Summary",
        "",
        f"- **Mode:** {'LIVE' if summary.get('apply') else 'DRY-RUN'}",
        f"- **Requests:** {summary.get('total_requests')}",
        f"- **Routes Probed:** {summary.get('routes_probed')}",
        f"- **Elapsed Time:** {summary.get('elapsed_seconds')}s",
    ]
    if summary.get("phase_4_total_evaluations", 0) > 0:
        rate = summary.get("phase_4_agreement_rate")
        pct = f"{rate * 100:.1f}%" if rate is not None else "N/A"
        evals = summary.get("phase_4_total_evaluations")
        lines.append(f"- **Phase 4 Agreement Rate:** {pct} ({evals} 429s evaluated)")
    lines.extend(
        [
            "",
            "| Route | Provider | Status | Failure Class | Rule ID |",
            "|---|---|---|---|---|",
        ]
    )

    for r in routes:
        route_id = r.get("route_id", "")
        provider = r.get("provider", "")
        obs = r.get("observations", {})
        p0 = obs.get("phase_0", {})
        status = p0.get("status") or r.get("status") or "-"
        cls = p0.get("classification") or {}
        f_class = cls.get("failure_class", "-")
        rule_id = cls.get("rule_id", "-")
        lines.append(f"| `{route_id}` | `{provider}` | {status} | `{f_class}` | `{rule_id}` |")

    Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LLM endpoint rate-limit characterization probe harness"
    )
    parser.add_argument(
        "--route",
        action="append",
        dest="routes",
        help="Route ID to probe (repeatable; default: all routes)",
    )
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="Provider to probe (repeatable; expands to that provider's routes)",
    )
    parser.add_argument(
        "--phase",
        action="append",
        dest="phases",
        choices=["0", "1", "1b", "2", "3", "4"],
        help="Probe phase to run (repeatable; default: 0)",
    )
    parser.add_argument(
        "--include-paid",
        action="store_true",
        default=False,
        help="Include paid routes (default: free routes only)",
    )
    parser.add_argument(
        "--max-requests-per-route",
        type=int,
        default=40,
        help="Safety ceiling per route (default: 40)",
    )
    parser.add_argument(
        "--max-requests-total",
        type=int,
        default=400,
        help="Safety ceiling across all routes (default: 400)",
    )
    parser.add_argument(
        "--max-wall-seconds",
        type=int,
        default=900,
        help="Safety wall-clock timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("llm_route_characterization.json"),
        help="Output report JSON path (default: llm_route_characterization.json)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute live HTTP calls (without this, dry-run only)",
    )
    parser.add_argument(
        "--endurance",
        action="store_true",
        default=False,
        help=(
            "Endurance mode: poll every selected route at least once per --endurance-interval "
            "until it returns a 200 or --endurance-hours elapses. A throttle is never terminal. "
            "Routes that never succeed are reported as unreliable and recommended for disable; "
            "routes that succeed even once get a throttle-tolerant input-ceiling measurement."
        ),
    )
    parser.add_argument(
        "--endurance-hours",
        type=float,
        default=3.0,
        help="Endurance window in hours (default: 3)",
    )
    parser.add_argument(
        "--endurance-interval",
        type=float,
        default=60.0,
        help="Minimum seconds between attempts on one route (default: 60)",
    )

    args = parser.parse_args(argv)
    phases = args.phases or ["0"]

    catalog = load_route_catalog()
    selected: list[dict[str, Any]] = []

    for route in catalog:
        if not args.include_paid and not route.get("free"):
            continue
        if args.providers and route.get("provider") not in args.providers:
            continue
        if args.routes and route.get("route_id") not in args.routes:
            continue
        selected.append(route)

    print(
        f"LLM Rate Probe: selected {len(selected)} routes across "
        f"{len(set(r.get('provider') for r in selected))} providers. "
        f"Phases: {', '.join(phases)}. "
        f"Mode: {'LIVE (--apply)' if args.apply else 'DRY RUN'}"
    )

    if args.endurance:
        report = run_endurance(
            selected,
            apply=args.apply,
            hours=args.endurance_hours,
            interval_seconds=args.endurance_interval,
        )
    else:
        runner = RateProbeRunner(
            apply=args.apply,
            max_requests_per_route=args.max_requests_per_route,
            max_requests_total=args.max_requests_total,
            max_wall_seconds=args.max_wall_seconds,
        )
        report = run_probes(selected, phases, runner)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not args.endurance:
        emit_github_step_summary(report)

    print(f"Probe complete. Report saved to {args.out}")
    if not args.apply:
        print(
            "\nNote: Ran in DRY-RUN mode (0 live requests issued). Pass --apply to execute probes."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
