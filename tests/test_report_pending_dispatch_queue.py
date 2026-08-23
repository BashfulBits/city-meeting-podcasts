from __future__ import annotations

import json
from datetime import UTC, datetime

from scripts.report_pending_dispatch_queue import (
    _candidate_route_ids,
    _priority_label,
    _route_status,
    build_report,
)

DISPATCH_LIMITS = {
    "model_aliases": {"mistral/alias": "mistral/real"},
    "model_routes_map": {
        "mistral/real": ["mistral_route_a"],
        "gemini/flash": ["gemini_route_a", "gemini_route_b"],
    },
    "routes_by_id": {
        "mistral_route_a": {"provider": "mistral", "rpd": 0},
        "gemini_route_a": {"provider": "gemini", "rpd": 500},
        "gemini_route_b": {"provider": "gemini", "rpd": 500},
    },
}


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self) -> bytes:
        return self.value


class _NoSuchKey(Exception):
    pass


class _Exceptions:
    NoSuchKey = _NoSuchKey


class _Paginator:
    def __init__(self, pages: list[dict]):
        self.pages = pages

    def paginate(self, **_kwargs):
        return iter(self.pages)


class _Client:
    def __init__(self, *, heads: dict[str, dict], pages: list[dict], budget: dict | None = None):
        self.heads = heads
        self.paginator = _Paginator(pages)
        self.exceptions = _Exceptions()
        self._budget = budget

    def get_paginator(self, _name: str):
        return self.paginator

    def head_object(self, *, Bucket: str, Key: str):
        del Bucket
        return {"Metadata": self.heads[Key]}

    def get_object(self, *, Bucket: str, Key: str):
        del Bucket, Key
        if self._budget is None:
            raise self.exceptions.NoSuchKey()
        return {"Body": _Body(json.dumps(self._budget).encode())}


def _page(*keys: str) -> dict:
    return {"Contents": [{"Key": key} for key in keys]}


def _meta(record_id: str, model: str, *, allowed_models: list[str] | None = None) -> dict:
    policy = {"allowed_models": allowed_models} if allowed_models else {}
    return {
        "id": record_id,
        "model": model,
        "created_at": "2026-08-19T00:00:00Z",
        "available_at": "2026-08-19T00:00:00Z",
        "policy": json.dumps(policy),
    }


def test_candidate_route_ids_resolves_aliases_and_dedupes():
    ids = _candidate_route_ids(["mistral/alias", "mistral/real"], DISPATCH_LIMITS)
    assert ids == ["mistral_route_a"]


def test_route_status_flags_rpd_zero_as_paused():
    status = _route_status("mistral_route_a", DISPATCH_LIMITS, {"routes": {}}, datetime.now(UTC))
    assert status["state"] == "paused_rpd0"


def test_route_status_reports_available_with_no_ledger_entry():
    status = _route_status("gemini_route_a", DISPATCH_LIMITS, {"routes": {}}, datetime.now(UTC))
    assert status["state"] == "reported_available"


def test_route_status_honors_blocked_until():
    now = datetime(2026, 8, 19, tzinfo=UTC)
    budget = {"routes": {"gemini_route_a": {"blocked_until": "2026-08-19T12:00:00Z"}}}
    status = _route_status("gemini_route_a", DISPATCH_LIMITS, budget, now)
    assert status["state"] == "blocked_until"


def test_priority_label_reflects_policy():
    assert _priority_label({"submit_next": True}) == "0-urgent"
    assert _priority_label({"timeout_class": "long"}) == "2-long"
    assert _priority_label({}) == "1-fast"


def test_build_report_marks_mistral_only_job_stuck_and_mixed_job_eligible(monkeypatch):
    monkeypatch.setattr(
        "scripts.report_pending_dispatch_queue._load_dispatch_limits",
        lambda: DISPATCH_LIMITS,
    )
    keys = [
        "ready/000-0-urgent-000-mistral-job.json",
        "ready/001-1-fast-001-gemini-job.json",
    ]
    heads = {
        keys[0]: _meta("mistral-job", "mistral/real"),
        keys[1]: _meta("gemini-job", "gemini/flash"),
    }
    client = _Client(heads=heads, pages=[_page(*keys)])

    rows = build_report(client, "dispatch", limit=10, workers=2, now=datetime.now(UTC))

    assert [r["id"] for r in rows] == ["mistral-job", "gemini-job"]
    mistral_row, gemini_row = rows
    assert mistral_row["verdict"] == "STUCK"
    assert mistral_row["mistral_only"] is True
    assert gemini_row["verdict"] == "ELIGIBLE"
    assert gemini_row["mistral_only"] is False


def test_build_report_flags_beyond_lookahead(monkeypatch):
    monkeypatch.setattr(
        "scripts.report_pending_dispatch_queue._load_dispatch_limits",
        lambda: DISPATCH_LIMITS,
    )
    keys = [f"ready/{i:03d}-1-fast-000-job{i}.json" for i in range(502)]
    heads = {key: _meta(f"job{i}", "gemini/flash") for i, key in enumerate(keys)}
    client = _Client(heads=heads, pages=[_page(*keys)])

    rows = build_report(client, "dispatch", limit=502, workers=4, now=datetime.now(UTC))

    assert rows[499]["within_lookahead"] is True
    assert rows[500]["within_lookahead"] is False


def test_build_report_skips_unreadable_marker(monkeypatch):
    monkeypatch.setattr(
        "scripts.report_pending_dispatch_queue._load_dispatch_limits",
        lambda: DISPATCH_LIMITS,
    )
    # No Metadata entry for the first key -- head_object raises a KeyError from the test double,
    # exercising _head_marker's catch-and-warn path so one unreadable marker can't sink the report.
    keys = ["ready/000-broken.json", "ready/001-ok.json"]
    heads = {keys[1]: _meta("ok-job", "gemini/flash")}
    client = _Client(heads=heads, pages=[_page(*keys)])

    rows = build_report(client, "dispatch", limit=10, workers=2, now=datetime.now(UTC))
    assert [r["id"] for r in rows] == ["ok-job"]
