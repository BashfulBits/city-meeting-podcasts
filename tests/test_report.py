"""Tests for the resource report (JSON/Markdown/admin page) + JS↔Python model parity."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from citypods.models import City
from citypods.report import build_report, to_admin_html, to_markdown


def _city(slug, provider, extract=False):
    return City(
        slug=slug,
        provider=provider,
        source={"feed_url": "u"} if provider != "swagit" else {"list_url": "u", "body": "b"},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
        extract_audio=extract,
    )


def _cities():
    # 2 hosted (swagit HLS + extract_audio granicus) + 2 not hosted (plain granicus)
    return [
        _city("a", "swagit"),
        _city("b", "granicus", extract=True),
        _city("c", "granicus"),
        _city("d", "granicus"),
    ]


SITE = {"defaults": {"max_episodes": 50, "audio_max_kbps": 96, "materialize_budget_per_run": 25}}


def test_build_report_measures_host_fraction():
    rep = build_report(_cities(), site_config=SITE)
    assert rep["generated_for_feeds"] == 4
    # 2 of 4 hosted -> host_frac 0.5
    assert rep["current"]["inputs"]["host_frac"] == 0.5
    assert rep["current"]["cap_is_bottleneck"] is True
    assert "1000" in rep["scale_scenarios"]


def test_markdown_summary_has_cost_and_bottleneck():
    md = to_markdown(build_report(_cities(), site_config=SITE))
    assert "$" in md and "/mo" in md
    assert "bottleneck" in md.lower()
    assert "| Feeds |" in md


def test_admin_html_substitutes_and_embeds_valid_json():
    html = to_admin_html(build_report(_cities(), site_config=SITE))
    assert "__REPORT_JSON__" not in html and "__SEED_JSON__" not in html
    m = re.search(r'<script id="report" type="application/json">(.*?)</script>', html, re.S)
    assert m and json.loads(m.group(1))["generated_for_feeds"] == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available for JS parity")
def test_js_python_parity():
    """The admin page re-implements project() in JS; assert it matches Python for sample inputs."""
    from citypods.projection import ModelInputs, project

    html = to_admin_html(build_report(_cities(), site_config=SITE))
    js_fn = re.search(r"function project\(i\)\{.*?\n\}", html, re.S).group(0)
    # project() references module-level consts in the page; include them so node can run it.
    consts = re.search(r"const B2_GB_MO\s*=.*?;", html, re.S).group(0)
    js_fn = consts + "\n" + js_fn
    cases = [
        dict(
            feeds=1000,
            episodes_per_feed=50,
            duration_hours=2,
            kbps=96,
            host_frac=1,
            sec_per_ep=90,
            cycle_hours=6,
            time_budget_hours=5,
            safety=0.8,
            per_run_cap=0,
            meetings_per_week=1,
        ),
        dict(
            feeds=80,
            episodes_per_feed=50,
            duration_hours=2,
            kbps=96,
            host_frac=0.5,
            sec_per_ep=120,
            cycle_hours=6,
            time_budget_hours=5,
            safety=0.8,
            per_run_cap=25,
            meetings_per_week=1,
        ),
    ]
    emit = (
        "console.log(JSON.stringify(cases.map(project).map("
        "r=>[Math.round(r.monthly*100),r.through,Math.round(r.backfillDays)])));"
    )
    script = js_fn + "\nconst cases=" + json.dumps(cases) + ";\n" + emit
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    js = json.loads(out)
    for case, (jcost, jthrough, jdays) in zip(cases, js, strict=True):
        cap = case["per_run_cap"] or None
        p = project(ModelInputs(**{**case, "per_run_cap": cap}))
        assert round(p.monthly_cost_usd * 100) == jcost
        assert p.per_run_throughput == jthrough
        assert round(p.full_backfill_days) == jdays
