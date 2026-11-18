import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).parents[1] / "workers" / "decision-batch" / "karpo_stream_probe.py"


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setenv("CITYPODS_BATCH_BUCKET", "bucket-for-tests")
    monkeypatch.setenv("CITYPODS_MODEL_BUCKET", "model-bucket-for-tests")
    spec = importlib.util.spec_from_file_location("karpo_stream_probe_under_test", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stream_mode_requires_manifest_and_positive_chapters_before_any_cloud_call(
    probe, monkeypatch
):
    def no_credentials():
        raise AssertionError("credentials requested before argument validation")

    monkeypatch.setattr(probe, "metadata_access_token", no_credentials)
    for argv in (["stream", "12B"], ["stream", "12B", "--manifest", "k", "--chapters", "0"]):
        monkeypatch.setattr(sys, "argv", ["probe", *argv])
        with pytest.raises(SystemExit) as exit_info:
            probe.main()
        assert exit_info.value.code == 2


def test_phase0_comparison_pairs_by_chapter_not_position(probe, tmp_path):
    def request(chapter, value):
        return {"chapter": chapter, "grouped": {"p": {"tag": value}}}

    baseline = {
        "mode": "fp8_layerwise",
        "stages": {"tests_fp8": {"requests": [request("a", 0.2), request("b", 0.9)]}},
    }
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    ours = [{"chapter": "b", "p": {"tag": 0.9}}, {"chapter": "a", "p": {"tag": 0.2}}]
    result = probe.compare_phase0(ours, str(path))
    assert result["identical_pairs"] == 2 and result["max_abs_diff"] == 0
    with pytest.raises(ValueError, match="no result for chapter"):
        probe.compare_phase0([{"chapter": "z", "p": {"tag": 0.1}}], str(path))


def test_run_ids_are_unique_per_process_and_keys_use_them(probe):
    assert len(probe.RUN_ID) == 12
    written = []

    class Store:
        def put(self, key, data, if_generation_match):
            written.append((key, if_generation_match))

    probe.emit(Store(), "stream-12B", {"x": 1})
    assert written == [(f"attempts/r60/stream-12B-{probe.RUN_ID}.json", 0)]
