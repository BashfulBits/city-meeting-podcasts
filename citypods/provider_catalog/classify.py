"""Turn one canary response into a Verdict: universal rules first, then the provider's signals.

The universal rules live only here and cannot be overridden by a provider plugin:

* a timeout or transport error is always ``inconclusive`` -- a slow provider is not a dead one;
* a 2xx whose first streamed event is not an error is ``proven``;
* a 2xx whose first event IS an error falls through to the provider's signals, like any failure;
* anything no provider signal recognizes is ``inconclusive``.
"""

from __future__ import annotations

from dataclasses import dataclass

from citypods.provider_catalog.rules import ProviderRules, Response, Verdict


@dataclass(frozen=True)
class Classification:
    verdict: Verdict
    status: int | None
    reason: str  # report-safe: a signal note or a universal rule name, never a response body


def classify(response: Response, rules: ProviderRules) -> Classification:
    if response.timed_out:
        return Classification("inconclusive", None, "timeout (never evidence)")
    if response.transport_error:
        return Classification("inconclusive", None, f"transport: {response.transport_error}")
    status = response.status
    if status is not None and 200 <= status < 300 and not response.first_event_error:
        return Classification("proven", status, "completion streamed")
    for signal in rules.signals:
        if signal.matches(response):
            return Classification(signal.verdict, status, signal.note or signal.verdict)
    return Classification("inconclusive", status, "unrecognized response")
