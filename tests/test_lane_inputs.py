from __future__ import annotations

import tomllib

from scripts import lane_inputs
from scripts.lane_inputs import LANES, ROOT, render


def test_lane_inputs_are_current():
    assert lane_inputs.main(["--check"]) == 0


def test_every_lane_extra_exists_and_is_rendered():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    for lane, extras in LANES.items():
        lines = render(lane).splitlines()
        for req in project["dependencies"]:
            assert req in lines, (lane, req)
        for extra in extras:
            for req in project["optional-dependencies"][extra]:
                assert req in lines, (lane, extra, req)


def test_only_llm_lanes_carry_litellm():
    carrying = {
        lane for lane in LANES if any(r.startswith("litellm") for r in render(lane).splitlines())
    }
    assert carrying == {"prod", "dev"}
