"""The Worker's non-secret tuning compiles from config/dispatch_tuning.yml; no JSON drift."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "compile_dispatch_tuning", REPO_ROOT / "scripts" / "compile_dispatch_tuning.py"
)
compile_dispatch_tuning = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compile_dispatch_tuning)


def test_committed_json_matches_the_yaml():
    values = compile_dispatch_tuning.load_tuning()
    assert compile_dispatch_tuning.OUTPUT_JSON.read_text(
        encoding="utf-8"
    ) == compile_dispatch_tuning.render(values)
    assert compile_dispatch_tuning.main(["--check"]) == 0


def test_render_is_sorted_and_numeric():
    rendered = json.loads(compile_dispatch_tuning.render({"B_NAME": 2, "A_NAME": 1}))
    assert list(rendered["values"]) == ["A_NAME", "B_NAME"]
    assert all(isinstance(v, int) for v in rendered["values"].values())


@pytest.mark.parametrize(
    "body, message",
    [
        ("lower_case: 1\n", "UPPER_SNAKE_CASE"),
        ("SOME_API_KEY: 1\n", "credential or account/URL"),
        ("AI_GATEWAY_ID: 1\n", "credential or account/URL"),
        ("MAX_THING: -1\n", "non-negative integer"),
        ("MAX_THING: hold\n", "non-negative integer"),
        ("MAX_THING: true\n", "non-negative integer"),
    ],
)
def test_rejects_bad_entries(tmp_path, body, message):
    path = tmp_path / "t.yml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match=message):
        compile_dispatch_tuning.load_tuning(path)


def test_check_fails_when_the_json_is_stale(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "dispatch_tuning.json"
    stale.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(compile_dispatch_tuning, "OUTPUT_JSON", stale)
    assert compile_dispatch_tuning.main(["--check"]) == 1
    assert "out of date" in capsys.readouterr().err
