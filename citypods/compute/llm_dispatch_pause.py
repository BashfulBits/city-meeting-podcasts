"""Pause LLM Dispatch v2 claims around an out-of-band provider probe.

A catalog canary or rate probe that calls a provider directly competes with production dispatch
for the same account/model limits, so its 429s say nothing about the model. ``paused(...)`` stops
the v2 Worker from claiming NEW work for one route, one provider, or everything; waits until the
selection's in-flight (leased) jobs drain; and resumes on exit. Every pause also carries a
Worker-side expiry, so a crashed caller cannot leave dispatch halted.

Calls made while paused should be charged to the route with :func:`reserve` so production pacing
counts them against the route's rpm/rpd ledger.

CLI (credentials from ``LLM_DISPATCH_V2_URL`` / ``LLM_DISPATCH_V2_AUTH_TOKEN``)::

    python -m citypods.compute.llm_dispatch_pause pause --provider nvidia --seconds 600
    python -m citypods.compute.llm_dispatch_pause status --provider nvidia
    python -m citypods.compute.llm_dispatch_pause resume --provider nvidia
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import requests

MAX_PAUSE_SECONDS = 3600


class DispatchPauseError(RuntimeError):
    """The Worker refused or failed a pause-control request."""


@dataclass(frozen=True)
class Selection:
    scope: str  # "global" | "provider" | "route"
    target: str | None = None

    def body(self) -> dict[str, Any]:
        if self.scope == "global":
            return {"scope": "global"}
        return {"scope": self.scope, "target": self.target}


@dataclass
class PauseOutcome:
    """What the caller needs to interpret its probe results.

    ``pause_expired`` is set when the ``paused(...)`` block exits: it is true if the pause could
    have lapsed before the probe finished, in which case production claims may have resumed
    underneath it. Read ``contended`` after the block, not inside it.
    """

    selection: Selection
    drained: bool
    in_flight_at_start: int
    waited_seconds: float
    pause_expired: bool = False
    # Re-arms the pause for another full window; call it between steps of a long probe.
    renew: Callable[[], None] = field(default=lambda: None, repr=False, compare=False)

    @property
    def contended(self) -> bool:
        """Probe results may reflect production traffic, not the model alone."""
        return not self.drained or self.pause_expired


@dataclass(frozen=True)
class ContextSession:
    """Server-owned weekly allowance; a repeated start never extends its deadline."""

    week_start: str
    run_id: str
    deadline_ms: int
    remaining_input: int
    remaining_output: int
    remaining_requests: int


@dataclass(frozen=True)
class ContextAdmission:
    """Single-use permission. Lost responses must be abandoned, never retried."""

    attempt_id: str


class DispatchPauseClient:
    def rate_failures(self) -> dict[str, Any]:
        """Fetch bounded current-day counters; older Workers and partial results defer checks."""
        data = self._request("GET", "v2/stats?rate_failures=1")
        try:
            day = data["utc_day"]
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("invalid UTC day")
            rows = data["route_failures"]
            if (
                data.get("kind") != "rate_failures"
                or data.get("truncated") is not False
                or not isinstance(rows, list)
                or len(rows) > 384
            ):
                raise ValueError("unavailable bounded counters")
            for row in rows:
                if (
                    row["utc_day"] != day
                    or not isinstance(row["route_id"], str)
                    or not row["route_id"]
                    or row["failure_class"] not in {"own_rpm", "own_tpm", "unknown_429"}
                    or type(row["count"]) is not int
                    or row["count"] < 0
                ):
                    raise ValueError("invalid failure cell")
        except (ValueError, TypeError, KeyError) as exc:
            raise DispatchPauseError("rate failure telemetry unavailable") from exc
        return data

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = 20,
    ) -> None:
        self.url = (
            url
            or os.environ.get("CITYPODS_LLM_DISPATCH_V2_URL")
            or os.environ.get("LLM_DISPATCH_V2_URL")
        )
        self.token = (
            token
            or os.environ.get("CITYPODS_LLM_DISPATCH_V2_AUTH_TOKEN")
            or os.environ.get("LLM_DISPATCH_V2_AUTH_TOKEN")
        )
        if not self.url:
            raise DispatchPauseError("LLM_DISPATCH_V2_URL is not set")
        # The bearer token must never travel in cleartext; plain HTTP is allowed only tokenless
        # (e.g. a local `wrangler dev`).
        if self.token:
            parts = urlsplit(self.url)
            if parts.scheme.lower() != "https" or not parts.netloc:
                raise DispatchPauseError(
                    "LLM_DISPATCH_V2_URL must be an https:// URL when an auth token is set"
                )
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict:
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        url = urljoin(self.url.rstrip("/") + "/", path)
        try:
            response = self.session.request(
                method, url, headers=headers, json=body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise DispatchPauseError(f"{path} request failed: {type(exc).__name__}") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or not isinstance(data, dict):
            detail = (data.get("detail") or data.get("error")) if isinstance(data, dict) else None
            raise DispatchPauseError(f"{path} returned HTTP {response.status_code}: {detail}")
        return data

    def pause(self, selection: Selection, seconds: int, reason: str = "") -> dict:
        if not 1 <= seconds <= MAX_PAUSE_SECONDS:
            raise DispatchPauseError(f"seconds must be 1..{MAX_PAUSE_SECONDS}")
        body = selection.body() | {"seconds": seconds, "reason": reason}
        return self._request("POST", "v2/dispatch:pause", body)

    def resume(self, selection: Selection) -> dict:
        return self._request("POST", "v2/dispatch:resume", selection.body())

    def status(self, selection: Selection) -> dict:
        query = urlencode({k: v for k, v in selection.body().items() if v is not None})
        return self._request("GET", f"v2/dispatch:pause-status?{query}")

    def in_flight(self, selection: Selection) -> int:
        """The selection's live leased-job count; a malformed status is an error, never zero."""
        value = self.status(selection).get("in_flight")
        if type(value) is not int or value < 0:
            raise DispatchPauseError("pause-status did not return a non-negative in_flight count")
        return value

    def reserve(self, route_id: str, requests_made: int = 1) -> dict:
        return self._request(
            "POST", "v2/dispatch:reserve", {"route_id": route_id, "requests": requests_made}
        )

    @staticmethod
    def _context_session(data: Mapping[str, Any]) -> ContextSession:
        fields = ("deadline_ms", "remaining_input", "remaining_output", "remaining_requests")
        if (
            not isinstance(data.get("week_start"), str)
            or not isinstance(data.get("run_id"), str)
            or any(type(data.get(k)) is not int or data[k] < 0 for k in fields)
            or data["deadline_ms"] <= 0
            or not re.fullmatch(r"[1-9][0-9]{0,19}", data["run_id"])
            or data["remaining_input"] > 2_097_152
            or data["remaining_output"] > 131_072
            or data["remaining_requests"] > 24
        ):
            raise DispatchPauseError("invalid context session response")
        try:
            week = date.fromisoformat(data["week_start"])
            if week.weekday() != 0 or week.isoformat() != data["week_start"]:
                raise ValueError("context week must start Monday")
        except ValueError as exc:
            raise DispatchPauseError("invalid context week") from exc
        return ContextSession(**{k: data[k] for k in ("week_start", "run_id", *fields)})

    def start_context(self, run_id: str, catalog_digest: str) -> ContextSession:
        data = self._request(
            "POST",
            "v2/dispatch:reserve",
            {"operation": "context_start", "run_id": run_id, "catalog_digest": catalog_digest},
        )
        if data.get("ok") is not True or data.get("run_id") != run_id:
            raise DispatchPauseError("context session was not admitted")
        return self._context_session(data)

    def reserve_context(
        self,
        *,
        run_id: str,
        catalog_digest: str,
        route_id: str,
        dimension: str,
        attempt_id: str,
        input_tokens: int,
        output_tokens: int,
        request_digest: str,
    ) -> ContextAdmission:
        data = self._request(
            "POST",
            "v2/dispatch:reserve",
            {
                "operation": "context_admit",
                "run_id": run_id,
                "catalog_digest": catalog_digest,
                "route_id": route_id,
                "dimension": dimension,
                "attempt_id": attempt_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "request_digest": request_digest,
            },
        )
        if (
            data.get("ok") is not True
            or data.get("disposition") != "new"
            or data.get("attempt_id") != attempt_id
        ):
            raise DispatchPauseError("context attempt has no new single-use admission")
        return ContextAdmission(attempt_id)

    def start_manual_context(self, run_id: str, catalog_digest: str, **limits) -> ContextSession:
        data = self._request(
            "POST",
            "v2/dispatch:reserve",
            {
                "operation": "context_manual_start",
                "run_id": run_id,
                "catalog_digest": catalog_digest,
                **limits,
            },
        )
        if data.get("ok") is not True or data.get("run_id") != run_id:
            raise DispatchPauseError("context session was not admitted")
        return self._context_session(data)

    def reserve_manual_context(
        self,
        *,
        run_id: str,
        catalog_digest: str,
        route_id: str,
        dimension: str,
        attempt_id: str,
        input_tokens: int,
        output_tokens: int,
        request_digest: str,
    ) -> ContextAdmission:
        data = self._request(
            "POST",
            "v2/dispatch:reserve",
            {
                "operation": "context_manual_admit",
                "run_id": run_id,
                "catalog_digest": catalog_digest,
                "route_id": route_id,
                "dimension": dimension,
                "attempt_id": attempt_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "request_digest": request_digest,
            },
        )
        if (
            data.get("ok") is not True
            or data.get("disposition") != "new"
            or data.get("attempt_id") != attempt_id
        ):
            raise DispatchPauseError("context attempt has no new single-use admission")
        return ContextAdmission(attempt_id)

    def finish_manual_context(self, run_id: str, catalog_digest: str) -> ContextSession:
        data = self._request(
            "POST",
            "v2/dispatch:reserve",
            {
                "operation": "context_manual_finish",
                "run_id": run_id,
                "catalog_digest": catalog_digest,
            },
        )
        if data.get("ok") is not True or data.get("run_id") != run_id:
            raise DispatchPauseError("manual context session cleanup unavailable")
        return self._context_session(data)

    def context_status(self, selection: Selection) -> dict:
        query = urlencode(selection.body() | {"context": "1"})
        data = self._request("GET", f"v2/dispatch:pause-status?{query}")
        context = data.get("context")
        if (
            data.get("ok") is not True
            or not isinstance(context, dict)
            or type(context.get("enabled")) is not bool
            or not isinstance(context.get("catalog_digest"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", context["catalog_digest"])
            or not isinstance(context.get("routes"), dict)
            or "session" not in context
        ):
            raise DispatchPauseError("invalid context status response")
        if context["session"] is not None:
            self._context_session(context["session"])
        return context


@contextmanager
def paused(
    selection: Selection,
    *,
    seconds: int = 900,
    drain_timeout: float = 300,
    poll_interval: float = 10,
    reason: str = "provider probe",
    client: DispatchPauseClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[PauseOutcome]:
    """Pause ``selection``, wait for its in-flight work to drain, and always resume afterwards.

    The pause is kept armed for the whole drain and probe: it is renewed whenever it would lapse
    before the next drain poll, and once more when the drain ends so the probe starts with a full
    ``seconds`` window (``outcome.renew()`` re-arms it again for a longer probe). Expiry is
    tracked on the local clock from *before* each pause request, so it never outlives the Worker's.

    ``drain_timeout`` bounds the wait; if it passes, the body still runs and the outcome is marked
    ``contended`` so the caller can treat its results as inconclusive rather than as evidence. The
    outcome is also contended if the pause could have lapsed before the body finished.
    """
    client = client or DispatchPauseClient()
    deadline = 0.0

    def arm() -> None:
        nonlocal deadline
        requested_at = clock()
        client.pause(selection, seconds, reason)
        deadline = requested_at + seconds

    arm()
    outcome: PauseOutcome | None = None
    try:
        start = clock()
        in_flight = client.in_flight(selection)
        first = in_flight
        while in_flight > 0 and clock() - start < drain_timeout:
            if deadline - clock() <= poll_interval:
                arm()
            sleep(poll_interval)
            in_flight = client.in_flight(selection)
        waited = round(clock() - start, 1)
        arm()  # the probe gets a full window, however long the drain took
        outcome = PauseOutcome(
            selection=selection,
            drained=in_flight == 0,
            in_flight_at_start=first,
            waited_seconds=waited,
            renew=arm,
        )
        yield outcome
    finally:
        if outcome is not None:
            outcome.pause_expired = clock() >= deadline
        try:
            client.resume(selection)
        except DispatchPauseError as exc:
            # The Worker-side expiry still ends the pause; never mask the caller's own exception.
            print(f"warning: resume failed ({exc}); pause will expire by itself", file=sys.stderr)


def _selection(args: argparse.Namespace) -> Selection:
    if args.route:
        return Selection("route", args.route)
    if args.provider:
        return Selection("provider", args.provider)
    return Selection("global")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["pause", "resume", "status", "reserve"])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--provider")
    target.add_argument("--route")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--reason", default="manual")
    parser.add_argument("--requests", type=int, default=1, help="reserve: calls to charge")
    args = parser.parse_args(argv)

    client = DispatchPauseClient()
    selection = _selection(args)
    if args.action == "pause":
        result = client.pause(selection, args.seconds, args.reason)
    elif args.action == "resume":
        result = client.resume(selection)
    elif args.action == "status":
        result = client.status(selection)
    else:
        if not args.route:
            parser.error("reserve requires --route")
        result = client.reserve(args.route, args.requests)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
