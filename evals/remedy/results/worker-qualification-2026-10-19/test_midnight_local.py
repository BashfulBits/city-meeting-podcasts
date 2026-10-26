"""Offline checks for the authorized one-shot pause/direct-call boundary."""

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("midnight_local", HERE / "midnight_local.py")
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)


class Client:
    def __init__(self):
        self.pauses = {}
        self.resumed = []
        self.reserved = []

    def status(self, selection):
        route_ids = [r[1].route_id for r in local.requests_for_run()]
        return {
            "ok": True,
            "in_flight": 0,
            "pauses": list(self.pauses.values()),
            "routes": {r: {"rpd_remaining": 1000} for r in route_ids},
        }

    def pause(self, selection, seconds, reason):
        assert selection.scope == "route"
        key = "route:" + selection.target
        self.pauses[key] = {
            "scope": key,
            "reason": reason,
            "paused_until": int((local.PAUSE_AT + timedelta(hours=1)).timestamp() * 1000),
        }
        return {"ok": True, **self.pauses[key]}

    def resume(self, selection):
        self.resumed.append(selection.target)
        self.pauses.pop("route:" + selection.target, None)
        return {"ok": True}

    def reserve(self, route, count):
        self.reserved.append(route)
        assert count == 1
        return {"ok": True}


def test_prepare_preserves_all_three_exact_profiles():
    jobs = local.requests_for_run()
    assert [r[0]["role"] for r in jobs] == ["sol", "glm_flash", "deepseek"]
    for row, route, payload in jobs:
        assert payload["model"] == row["upstream_model"] == route.upstream_model
        assert payload["max_tokens"] == 16384
        assert "temperature" not in payload
        assert "reasoning_effort" not in payload


def test_pause_window_rejects_early_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "WINDOW", tmp_path)
    monkeypatch.setattr(local, "now", lambda: local.PAUSE_AT - timedelta(seconds=1))
    client = Client()
    with pytest.raises(ValueError, match="Outside one-shot"):
        local.pause_phase(client, local.requests_for_run())
    assert not client.pauses


def test_run_without_acknowledged_pause_never_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "WINDOW", tmp_path)
    monkeypatch.setattr(local.requests, "post", lambda *a, **k: pytest.fail("Uncoordinated call"))
    with pytest.raises(RuntimeError, match="pause phase not completed"):
        local.run_phase(Client(), local.requests_for_run())


def test_exactly_three_calls_restore_owned_pauses_and_do_not_repeat(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "WINDOW", tmp_path)
    monkeypatch.setattr(local, "now", lambda: local.PAUSE_AT)
    monkeypatch.setattr(local.time, "sleep", lambda _: None)
    monkeypatch.setattr(local.signal, "signal", lambda *args: None)
    monkeypatch.setattr(local.signal, "setitimer", lambda *args: None)
    monkeypatch.setattr(local, "validate_source_url", lambda url: None)
    jobs = local.requests_for_run()
    for _, route, _ in jobs:
        monkeypatch.setenv(route.api_key_env, "test-secret-only")
    client = Client()
    local.pause_phase(client, jobs)
    assert len(client.pauses) == 3
    monkeypatch.setattr(local, "now", lambda: local.START_AT)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))

        class Reply:
            status_code = 503
            headers = {"Retry-After": "60"}

            @staticmethod
            def json():
                return {"error": "temporary", "reflected_secret": "test-secret-only"}

        return Reply()

    monkeypatch.setattr(local.requests, "post", post)
    local.run_phase(client, jobs)
    assert len(calls) == 3
    assert len(client.reserved) == 3
    assert len(client.resumed) == 3
    assert all(c[1]["allow_redirects"] is False for c in calls)
    for _, route, _ in jobs:
        data = json.loads(
            (
                tmp_path / f"{next(r[0]['role'] for r in jobs if r[1] == route)}-result.json"
            ).read_text()
        )
        assert data["calibration_quality"] is None
    assert "test-secret-only" not in "".join(p.read_text() for p in tmp_path.glob("*.json"))
    local.run_phase(client, jobs)
    assert len(calls) == 3


def test_restore_does_not_resume_foreign_pause(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "WINDOW", tmp_path)
    client = Client()
    _, route, _ = local.requests_for_run()[0]
    client.pauses["route:" + route.route_id] = {
        "scope": "route:" + route.route_id,
        "reason": "foreign-owner",
        "paused_until": 9999999999999,
    }
    local.restore(client, [{"route_id": route.route_id, "owned": True}])
    assert not client.resumed
    assert client.pauses
