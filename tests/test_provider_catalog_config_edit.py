"""YAML proposals preserve comments and have exactly the requested semantic delta."""

from dataclasses import replace
from pathlib import Path

import pytest

from citypods.provider_catalog.apply import SOURCE_PATHS, EditPlan
from citypods.provider_catalog.config_edit import apply_config_edits, load_config
from citypods.provider_catalog.evidence import digest

ROOT = Path(__file__).resolve().parents[1]


def source(backup="    backup_models: [old] # preserve inline\n"):
    return dict(
        zip(
            SOURCE_PATHS,
            [
                "# limits\nproviders: {}\nroutes:\n  - route_id: old\n    model: old\n# trailing\n",
                "# site\nllm_lanes:\n  lane:\n    models: [primary]\n"
                + backup
                + "  other:\n    models: [untouched]\n# tail\n",
                "# decisions\nignored: [] # existing comment\n\nacknowledged: []\n",
            ],
            strict=True,
        )
    )


def edit_plan(texts):
    return EditPlan(
        "base",
        tuple((p, digest(texts[p])) for p in SOURCE_PATHS),
        ((("route_id", "new"), ("model", "new")),),
        (("lane", "new"),),
        (("host", "new", "2026-10-08"),),
    )


@pytest.mark.parametrize(
    "backup",
    [
        "",
        "    backup_models: [] # empty comment\n",
        "    backup_models: [old] # preserve inline\n",
        "    backup_models:\n      - old # list comment\n",
    ],
)
def test_narrow_edits_preserve_comments_primary_and_unrelated_blocks(backup):
    texts = source(backup)
    output = apply_config_edits(texts, edit_plan(texts))
    assert load_config(output[SOURCE_PATHS[1]])["llm_lanes"]["lane"]["models"] == ["primary"]
    assert "  other:\n    models: [untouched]\n# tail\n" in output[SOURCE_PATHS[1]]
    for path in SOURCE_PATHS:
        for line in texts[path].splitlines():
            if "#" in line:
                assert line[line.index("#") :] in output[path]


def test_editor_is_idempotent_for_ignores_and_backups():
    texts = source()
    plan = replace(edit_plan(texts), routes=())
    output = apply_config_edits(texts, plan)
    again = replace(plan, config_hashes=tuple((p, digest(output[p])) for p in SOURCE_PATHS))
    assert apply_config_edits(output, again) == output


@pytest.mark.parametrize("text", ["x: 1\nx: 2\n", "x: &a []\ny: *a\n", "ignored: null\n"])
def test_duplicate_alias_and_null_list_fail_closed(text):
    texts = source()
    texts[SOURCE_PATHS[2]] = text
    with pytest.raises((ValueError, KeyError)):
        apply_config_edits(texts, edit_plan(texts))


def test_stale_config_is_not_edited():
    texts = source()
    plan = edit_plan(texts)
    texts[SOURCE_PATHS[1]] += "# concurrent edit\n"
    with pytest.raises(ValueError, match="changed"):
        apply_config_edits(texts, plan)


def test_real_config_layouts_support_narrow_append_without_any_other_delta():
    texts = {p: (ROOT / p).read_text() for p in SOURCE_PATHS}
    lane = "chapter-agenda"
    route = {
        "route_id": "test_only",
        "model": "test/new",
        "provider": "nvidia",
        "upstream_model": "test/new",
        "account_id": "primary",
        "input_context_limit": 1000,
        "output_context_limit": 100,
        "free": True,
    }
    plan = EditPlan(
        "base",
        tuple((p, digest(texts[p])) for p in SOURCE_PATHS),
        (tuple(route.items()),),
        ((lane, "test/new"),),
        (("nvidia", "test/new", "2026-10-08"),),
    )
    output = apply_config_edits(texts, plan)
    assert load_config(output[SOURCE_PATHS[0]])["routes"][-1] == route
    assert (
        load_config(output[SOURCE_PATHS[1]])["llm_lanes"][lane]["backup_models"][-1] == "test/new"
    )
    assert all(
        line in output[SOURCE_PATHS[0]]
        for line in texts[SOURCE_PATHS[0]].splitlines()
        if line.lstrip().startswith("#")
    )


def test_missing_backups_after_block_models_are_inserted_before_the_next_lane():
    texts = source(backup="")
    texts[SOURCE_PATHS[1]] = texts[SOURCE_PATHS[1]].replace(
        "models: [primary]", "models:\n      - primary"
    )
    output = apply_config_edits(texts, edit_plan(texts))
    assert load_config(output[SOURCE_PATHS[1]])["llm_lanes"]["lane"]["backup_models"] == ["new"]
    assert load_config(output[SOURCE_PATHS[1]])["llm_lanes"]["other"] == {"models": ["untouched"]}


def test_real_additive_route_compiles_offline(tmp_path, monkeypatch):
    from scripts import compile_llm_limits

    texts = {p: (ROOT / p).read_text() for p in SOURCE_PATHS}
    raw = load_config(texts[SOURCE_PATHS[0]])
    account = raw["providers"]["nvidia"]["accounts"][0]["id"]
    route = {
        "route_id": "catalog_test_new",
        "model": "creator/catalog-test",
        "provider": "nvidia",
        "upstream_model": "creator/catalog-test",
        "account_id": account,
        "input_context_limit": 100000,
        "output_context_limit": 10000,
        "structured_output_method": "prompt_only",
        "structured_output_verified_on": "2026-10-08",
        "rpm": 1,
        "concurrency": 1,
        "free": True,
    }
    plan = EditPlan(
        "base",
        tuple((p, digest(texts[p])) for p in SOURCE_PATHS),
        (tuple(route.items()),),
        (("chapter-agenda", route["model"]),),
    )
    output = apply_config_edits(texts, plan)
    path = tmp_path / "config/provider_limits.yml"
    path.parent.mkdir()
    path.write_text(output[SOURCE_PATHS[0]])
    monkeypatch.setattr(compile_llm_limits, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(compile_llm_limits, "INPUT_YAML", path)
    compiled = compile_llm_limits.compile_limits()
    assert compiled["routes_by_id"]["catalog_test_new"]["structured_output_method"] == "prompt_only"
    assert compiled["model_routes_map"][route["model"]] == ["catalog_test_new"]
