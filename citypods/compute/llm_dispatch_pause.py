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
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

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
    """What the caller needs to interpret its probe results."""

    selection: Selection
    drained: bool
    in_flight_at_start: int
    waited_seconds: float

    @property
    def contended(self) -> bool:
        """Probe results may reflect production traffic, not the model alone."""
        return not self.drained


class DispatchPauseClient:
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
            detail = data.get("detail") if isinstance(data, dict) else None
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

    def reserve(self, route_id: str, requests_made: int = 1) -> dict:
        return self._request(
            "POST", "v2/dispatch:reserve", {"route_id": route_id, "requests": requests_made}
        )


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

    ``drain_timeout`` bounds the wait; if it passes, the body still runs and the outcome is marked
    ``contended`` so the caller can treat its results as inconclusive rather than as evidence.
    """
    client = client or DispatchPauseClient()
    client.pause(selection, seconds, reason)
    try:
        start = clock()
        in_flight = int(client.status(selection).get("in_flight") or 0)
        first = in_flight
        while in_flight > 0 and clock() - start < drain_timeout:
            sleep(poll_interval)
            in_flight = int(client.status(selection).get("in_flight") or 0)
        yield PauseOutcome(
            selection=selection,
            drained=in_flight == 0,
            in_flight_at_start=first,
            waited_seconds=round(clock() - start, 1),
        )
    finally:
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
