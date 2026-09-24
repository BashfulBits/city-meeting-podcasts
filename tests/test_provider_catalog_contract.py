"""Extensibility contract for provider-catalog reconciliation (review/48 R2, R8).

Providers are plugins and lanes come from the registry, so adding or removing either must never
need an edit to the reconciler core. These tests fail with a pointer to the fix when that drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from citypods.provider_catalog.registry import all_rules

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "citypods" / "provider_catalog"
CORE_MODULES = (
    "classify.py",
    "decisions.py",
    "issue.py",
    "probe.py",
    "quality.py",
    "reconcile.py",
    "registry.py",
    "rules.py",
)


def _configured_providers() -> set[str]:
    limits = yaml.safe_load((ROOT / "config" / "provider_limits.yml").read_text(encoding="utf-8"))
    return set(limits["providers"])


def test_every_configured_provider_has_exactly_one_plugin_and_no_plugin_is_orphaned():
    plugins = set(all_rules())
    missing = _configured_providers() - plugins
    orphaned = plugins - _configured_providers()
    assert not missing, (
        f"add citypods/provider_catalog/providers/<name>.py for {sorted(missing)} "
        "(copy providers/_template.py; start observation-only)"
    )
    assert not orphaned, f"delete the plugin(s) for removed provider(s) {sorted(orphaned)}"


def test_core_modules_never_branch_on_a_provider_name():
    # Publisher names (AA creators such as "nvidia" or "mistral") may appear as data in
    # quality.py; what is forbidden is *branching* on a provider: `provider == "x"`,
    # `rules.name != "x"`, `provider in {"x", ...}`.
    names = "|".join(map(re.escape, sorted(all_rules(), key=len, reverse=True)))
    pattern = re.compile(
        rf"(?:provider|name)\s*(?:==|!=)\s*[\"']({names})[\"']"
        rf"|(?:provider|name)\s+(?:not\s+)?in\s*[{{(\[][^}})\]]*[\"']({names})[\"']"
    )
    for module in CORE_MODULES:
        source = (CORE / module).read_text(encoding="utf-8")
        hits = [a or b for a, b in pattern.findall(source)]
        assert not hits, f"{module} mentions provider(s) {hits}; move that knowledge to a plugin"


def test_the_template_is_not_loaded_as_a_provider():
    assert "example" not in all_rules()


def test_every_plugin_documents_its_evidence():
    for name, rules in all_rules().items():
        module = CORE / "providers" / f"{name}.py"
        assert module.read_text(encoding="utf-8").lstrip().startswith('"""'), name
        for signal in rules.signals:
            assert signal.note, f"{name}: every Signal needs a note naming its evidence"


def test_the_branching_check_would_catch_a_provider_special_case():
    names = "|".join(map(re.escape, all_rules()))
    probe = re.compile(rf"(?:provider|name)\s*(?:==|!=)\s*[\"']({names})[\"']")
    assert probe.search('if provider == "gemini":')
