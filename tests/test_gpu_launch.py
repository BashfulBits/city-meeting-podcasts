"""Offline checks of the reusable GPU launcher: planner, kernel cache, calibration and launch."""

import copy
import json
from pathlib import Path

import pytest

from citypods.gpu_launch import calibrate, kernel_cache
from citypods.gpu_launch.launch import SplitRequired, plan_and_launch
from citypods.gpu_launch.planner import NoFeasibleGpu, estimate_padded_tokens, plan_launch
from citypods.gpu_launch.profiles import (
    GPU_CLASSES,
    Calibration,
    ModelProfile,
    load_profiles,
)
from citypods.gpu_launch.warmup import run_warmup, shape_tokens, warm_shapes
from citypods.judging.decision_batch_transport import PreconditionFailed
from scripts.eval_decision_batch import frozen_items

DIGEST = "sha256:" + "a" * 64


def calib(**overrides):
    base = {
        "load_seconds": 90.0,
        "load_seconds_p90": 120.0,
        "startup_seconds": 15.0,
        "tokens_per_second": 1000.0,
        "cold_overhead_seconds": 50.0,
        "config_overhead_seconds": 8.0,
        "token_budget": 16384,
        "peak_gpu_gib": 18.0,
        "confidence": "measured",
        "source": "test",
    }
    return Calibration(**{**base, **overrides})


def profile(**calibrations):
    return ModelProfile(
        "m",
        "qwen_fla",
        "rev",
        14.0,
        4096,
        calibrations or {"L4": calib(), "RTX_PRO_6000": calib(tokens_per_second=8000.0)},
    )


def test_shipped_profiles_validate_and_price_the_frozen_batch_on_the_l4():
    glide = load_profiles()["glide-9b"]
    plan = plan_launch(glide, 53_360)
    assert plan.chosen.gpu == "L4" and plan.chosen.launches == 1
    assert plan.chosen.total_seconds < 300
    assert plan.chosen.reservation_microdollars >= plan.chosen.cost_microdollars


def test_profile_loader_rejects_unknown_gpu_and_fields(tmp_path):
    document = json.loads(Path("config/gpu_profiles.json").read_text())
    bad = copy.deepcopy(document)
    bad["models"]["glide-9b"]["calibrations"]["H100"] = bad["models"]["glide-9b"]["calibrations"][
        "L4"
    ]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="unknown GPU"):
        load_profiles(path)
    extra = copy.deepcopy(document)
    extra["models"]["glide-9b"]["calibrations"]["L4"]["surprise"] = 1
    path.write_text(json.dumps(extra))
    with pytest.raises(ValueError, match="calibration fields"):
        load_profiles(path)


def test_planner_moves_to_the_faster_gpu_only_when_it_is_cost_competitive():
    p = profile()
    assert plan_launch(p, 30_000).chosen.gpu == "L4"
    big = plan_launch(p, 700_000, ceiling=1800)
    assert big.chosen.gpu == "RTX_PRO_6000"
    assert {c.gpu for c in big.candidates} == {"L4", "RTX_PRO_6000"}


def test_cache_modes_order_overhead_and_cost():
    def chosen(mode):
        return plan_launch(profile(), 50_000, gpus=["L4"], cache=mode).chosen

    archive, configs, none = chosen("archive"), chosen("configs"), chosen("none")
    assert none.seconds_per_launch == pytest.approx(archive.seconds_per_launch + 50, abs=0.2)
    assert configs.seconds_per_launch == pytest.approx(archive.seconds_per_launch + 8, abs=0.2)
    assert archive.cost_microdollars < configs.cost_microdollars < none.cost_microdollars
    with pytest.raises(ValueError, match="cache must be one of"):
        plan_launch(profile(), 1000, cache="warm")


def test_volume_beyond_the_ceiling_is_split_and_fixed_cost_repeats():
    plan = plan_launch(profile(), 1_200_000, gpus=["L4"], ceiling=600)
    chosen = plan.chosen
    assert chosen.launches >= 2
    assert chosen.timeout_seconds <= 600
    single = plan_launch(profile(), 300_000, gpus=["L4"], ceiling=600).chosen
    assert single.launches == 1
    assert chosen.total_seconds > single.total_seconds * 2  # fixed cost paid per launch


def test_timeout_and_reservation_follow_the_ledger_worst_case():
    chosen = plan_launch(profile(), 50_000, gpus=["L4"], margin=1.5).chosen
    assert chosen.timeout_seconds % 30 == 0
    assert chosen.timeout_seconds >= chosen.seconds_per_launch * 1.5
    assert chosen.reservation_microdollars == round(
        max(60, chosen.timeout_seconds) * GPU_CLASSES["L4"].rate + 0.4999
    )


def test_infeasible_candidates_explain_why():
    p = profile(L4=calib(peak_gpu_gib=30.0), RTX_PRO_6000=calib(confidence="estimated"))
    with pytest.raises(NoFeasibleGpu, match="does not fit.*estimate"):
        plan_launch(p, 10_000)
    assert plan_launch(p, 10_000, allow_estimated=True).chosen.gpu == "RTX_PRO_6000"
    with pytest.raises(NoFeasibleGpu, match="no calibration"):
        plan_launch(profile(RTX_PRO_6000=calib()), 1000, gpus=["L4"])
    with pytest.raises(NoFeasibleGpu, match="deadline"):
        plan_launch(profile(), 100_000, deadline_seconds=10)
    with pytest.raises(ValueError, match="unknown GPU"):
        plan_launch(profile(), 1000, gpus=["H100"])


def test_fixed_cost_over_the_ceiling_is_infeasible():
    p = profile(L4=calib(load_seconds=850.0, load_seconds_p90=900.0))
    with pytest.raises(NoFeasibleGpu, match="fixed startup"):
        plan_launch(p, 1000, gpus=["L4"], ceiling=900)


def test_token_estimator_tracks_the_frozen_batch():
    chars = []
    for row in frozen_items():
        q = row["question"]
        crit = q.get("criteria") or {}
        items = crit.items() if isinstance(crit, dict) else enumerate(crit)
        chars.append(
            len(row["state"])
            + len(q["instructions"])
            + sum(len(str(k)) + len(str(v)) for k, v in items)
        )
    assert estimate_padded_tokens(chars) == pytest.approx(53_360, rel=0.05)


def test_warm_shapes_cover_every_nb_and_the_row_sweep():
    shapes = warm_shapes("qwen_fla", 16384, 4096)
    nb = {-(-r * w // 2048) for r, w in shapes if w == 1024}
    assert nb == set(range(1, 9))
    assert {r for r, w in shapes if w == 24} == set(range(1, 33))
    assert shape_tokens(shapes) > 16384
    small = warm_shapes("qwen_fla", 4096, 4096)
    assert max(r * w for r, w in small if w == 1024) == 4096
    assert len(warm_shapes("generic", 16384, 4096)) == 3
    with pytest.raises(ValueError, match="unknown warm-up family"):
        warm_shapes("nope", 1, 1)


def test_run_warmup_calls_every_shape_and_syncs():
    calls, syncs = [], []
    ticks = iter([0.0, 2.5])
    report = run_warmup(
        lambda r, w: calls.append((r, w)),
        [(1, 8), (2, 8)],
        sync=lambda: syncs.append(1),
        clock=lambda: next(ticks),
    )
    assert calls == [(1, 8), (2, 8)] and syncs == [1]
    assert report == {"shapes": 2, "tokens": 24, "seconds": 2.5}


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.generation = 0

    def get(self, key, generation=None):
        item = self.objects.get(key)
        if item is None or (generation is not None and item[1] != generation):
            return None
        return item

    def put(self, key, data, if_generation_match):
        current = self.objects.get(key)
        if (current[1] if current else 0) != if_generation_match:
            raise PreconditionFailed(key)
        self.generation += 1
        self.objects[key] = (data, self.generation)
        return self.generation


def cache_tree(tmp_path):
    root = tmp_path / "cache"
    (root / "abc").mkdir(parents=True)
    (root / "abc" / "k.cubin").write_bytes(b"\x00cubin")
    (root / "def").mkdir()
    (root / "def" / "k.json").write_text("{}")
    return root


def test_archive_is_deterministic_and_round_trips(tmp_path):
    root = cache_tree(tmp_path)
    data = kernel_cache.build_archive(root)
    assert data == kernel_cache.build_archive(root)
    out = tmp_path / "out"
    entries = kernel_cache.restore_archive(
        data, out, expected_sha256=kernel_cache.archive_sha256(data)
    )
    assert entries == 2
    assert (out / "abc" / "k.cubin").read_bytes() == b"\x00cubin"


def test_restore_rejects_digest_mismatch_traversal_and_links(tmp_path):
    import io
    import tarfile

    root = cache_tree(tmp_path)
    data = kernel_cache.build_archive(root)
    with pytest.raises(ValueError, match="digest mismatch"):
        kernel_cache.restore_archive(data, tmp_path / "x", expected_sha256="0" * 64)
    assert not (tmp_path / "x").exists() or not any((tmp_path / "x").iterdir())

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    import gzip

    evil = gzip.compress(raw.getvalue())
    with pytest.raises(ValueError, match="unsafe"):
        kernel_cache.restore_archive(
            evil, tmp_path / "y", expected_sha256=kernel_cache.archive_sha256(evil)
        )
    (root / "link").symlink_to(root / "abc")
    with pytest.raises(ValueError, match="unsupported cache entry"):
        kernel_cache.build_archive(root)


def test_publish_pin_and_restore_fail_closed(tmp_path):
    store = MemoryStore()
    key = kernel_cache.cache_key(
        gpu_arch="sm_89", image_digest=DIGEST, model_revision="r", adapter="m", token_budget=16384
    )
    assert key != kernel_cache.cache_key(
        gpu_arch="sm_120", image_digest=DIGEST, model_revision="r", adapter="m", token_budget=16384
    )
    data = kernel_cache.build_archive(cache_tree(tmp_path))
    pin = kernel_cache.publish(store, key, data, gpu="L4", entries=2)
    assert kernel_cache.publish(store, key, data, gpu="L4", entries=2) == pin  # idempotent
    assert kernel_cache.current_pin(store, key) is None
    kernel_cache.set_current(store, pin)
    assert kernel_cache.current_pin(store, key) == pin
    info = kernel_cache.restore_pinned(store, pin, tmp_path / "r", gpu="L4")
    assert info["restored"] and info["entries"] == 2
    with pytest.raises(ValueError, match="different GPU"):
        kernel_cache.restore_pinned(store, pin, tmp_path / "r2", gpu="RTX_PRO_6000")
    store.objects[kernel_cache.object_key(key, pin["sha256"])] = (b"tampered", pin["generation"])
    with pytest.raises(ValueError, match="digest mismatch"):
        kernel_cache.restore_pinned(store, pin, tmp_path / "r3", gpu="L4")
    with pytest.raises(ValueError, match="pin fields"):
        kernel_cache.validate_pin({**pin, "extra": 1})
    with pytest.raises(ValueError, match="invalid kernel cache pin"):
        kernel_cache.validate_pin({**pin, "generation": 0})


def telemetry(warm=True, restored=False, tps_scale=1.0):
    calls = [
        {"padded_tokens": 16000, "seconds": 56.0, "warm_seconds": 8.0 if warm else None},
        {"padded_tokens": 16000, "seconds": 9.0, "warm_seconds": 8.0 if warm else None},
    ]
    return {
        "load_seconds": 90.0,
        "gpu_peak_bytes": int(17.5 * 1024**3),
        "kernel_cache": {"restored": restored},
        "profile": {"tokens": {"token_budget": 16384}, "forward": {"calls": calls}},
    }


def test_calibration_uses_warm_repeats_and_the_median():
    records = [
        calibrate.record_from_telemetry(telemetry(), model_id="m", gpu="L4"),
        calibrate.record_from_telemetry(telemetry(), model_id="m", gpu="L4"),
    ]
    records[1]["load_seconds"] = 120.0
    c = calibrate.calibrate(records, source="probe")
    assert c.tokens_per_second == 2000.0
    assert c.cold_overhead_seconds == 49.0  # (56 - 8) + (9 - 8)
    assert c.load_seconds == 105.0 and c.load_seconds_p90 == pytest.approx(
        156.0
    )  # 120 * 1.3 and c.load_seconds_p90 == 120.0
    assert c.peak_gpu_gib == 17.5 and c.token_budget == 16384


def test_calibration_from_a_restored_cache_and_without_warm_data():
    restored = calibrate.record_from_telemetry(
        telemetry(warm=False, restored=True), model_id="m", gpu="L4"
    )
    assert calibrate.calibrate([restored], source="s").cold_overhead_seconds == 0.0
    cold_only = calibrate.record_from_telemetry(telemetry(warm=False), model_id="m", gpu="L4")
    with pytest.raises(ValueError, match="no record has warm timings"):
        calibrate.calibrate([cold_only], source="s")
    other = {**restored, "gpu": "RTX_PRO_6000"}
    with pytest.raises(ValueError, match="share model"):
        calibrate.calibrate([restored, other], source="s")


class FakeJob:
    def __init__(self, fail=False):
        self.verified = []
        self.launched = []
        self.fail = fail

    def verify_job(self, digest, gpu):
        self.verified.append((digest, gpu.name))

    def launch(self, manifest_id, attempt_id, mode, task_timeout=None):
        self.launched.append((manifest_id, attempt_id, mode, task_timeout))
        return f"op-{attempt_id}"


def sealed_chunk(store):
    from citypods.judging.decision_batch_transport import put_chunk

    return put_chunk(
        store,
        frozen_items()[:3],
        max_rows=1000,
        max_bytes=8 * 1024 * 1024,
        max_estimated_tokens=3_000_000,
    )


def test_plan_and_launch_pins_cache_budget_gpu_and_timeout(tmp_path):
    store = MemoryStore()
    chunk = sealed_chunk(store)
    glide = load_profiles()["glide-9b"]
    key = kernel_cache.cache_key(
        gpu_arch="sm_89",
        image_digest=DIGEST,
        model_revision=glide.revision,
        adapter="glide-9b",
        token_budget=16384,
    )
    pin = kernel_cache.publish(
        store, key, kernel_cache.build_archive(cache_tree(tmp_path)), gpu="L4", entries=2
    )
    kernel_cache.set_current(store, pin)
    jobs = {"L4": FakeJob(), "RTX_PRO_6000": FakeJob()}
    plan, manifest_id, operation = plan_and_launch(
        store,
        jobs,
        chunks=[chunk],
        profile=glide,
        image_digest=DIGEST,
        attempt_id="att1",
        padded_tokens=2_000,
        windows={"eval": 1_000_000},
        cache="archive",
    )
    assert plan.chosen.gpu == "L4" and operation == "op-att1" and plan.cache == "archive"
    manifest = json.loads(store.get(f"manifests/{manifest_id}.json")[0])
    assert manifest["gpu"] == "L4" and manifest["kernel_cache"] == pin
    assert manifest["settings"]["token_budget"] == 16384
    launched = jobs["L4"].launched[0]
    assert launched[3] == plan.chosen.timeout_seconds and jobs["RTX_PRO_6000"].launched == []
    assert jobs["L4"].verified == [(DIGEST, "L4")]
    ledger = json.loads(store.get("dispatch/admission.json")[0])
    assert ledger["spent"] == {"eval": plan.chosen.reservation_microdollars}


def test_plan_and_launch_requires_a_published_cache_and_only_provisioned_gpus(tmp_path):
    store = MemoryStore()
    chunk = sealed_chunk(store)
    glide = load_profiles()["glide-9b"]
    kwargs = dict(
        chunks=[chunk], profile=glide, image_digest=DIGEST, attempt_id="a2", windows={"w": 10**7}
    )
    with pytest.raises(ValueError, match="no kernel cache published"):
        plan_and_launch(store, {"L4": FakeJob()}, padded_tokens=2_000, cache="archive", **kwargs)
    jobs = {"RTX_PRO_6000": FakeJob()}
    with pytest.raises(ValueError, match="no kernel cache"):
        plan_and_launch(store, jobs, padded_tokens=2_000, cache="archive", **kwargs)
    # The default needs no published archive: the image carries the fla configs.
    plan, manifest_id, _ = plan_and_launch(store, {"L4": FakeJob()}, padded_tokens=2_000, **kwargs)
    assert plan.cache == "configs"
    assert "kernel_cache" not in json.loads(store.get(f"manifests/{manifest_id}.json")[0])
    other = MemoryStore()  # one outstanding execution per ledger: use a fresh store
    kwargs["chunks"] = [sealed_chunk(other)]
    plan, _, _ = plan_and_launch(
        other, {"L4": FakeJob()}, padded_tokens=2_000, cache="none", **kwargs
    )
    assert plan.cache == "none"


def test_plan_and_launch_returns_split_plans_to_the_producer(tmp_path):
    store = MemoryStore()
    chunk = sealed_chunk(store)
    with pytest.raises(SplitRequired) as split:
        plan_and_launch(
            store,
            {"L4": FakeJob()},
            chunks=[chunk],
            profile=load_profiles()["glide-9b"],
            image_digest=DIGEST,
            attempt_id="a3",
            padded_tokens=5_000_000,
            windows={"w": 10**7},
            cache="none",
            ceiling=600,
        )
    assert split.value.plan.chosen.launches > 1


def _autotune_record(key, timings_by_warps):
    return {
        "key": key,
        "configs_timings": [
            [
                {"kwargs": {}, "num_warps": w, "num_ctas": 1, "num_stages": 3, "maxnreg": None},
                times,
            ]
            for w, times in timings_by_warps.items()
        ],
    }


def test_fla_configs_pick_the_fastest_config_and_a_deterministic_default(tmp_path):
    from citypods.gpu_launch import fla_configs

    cache = tmp_path / "cache"
    for name, key, timings in (
        ("a1", [10, 32, False], {1: [3.0, 3.0], 2: [1.0, 1.2], 4: [2.0, 2.0]}),
        ("b2", [20, 32, False], {1: [3.0], 2: [1.5], 4: [2.0]}),
        ("c3", [30, 32, False], {1: [0.5], 2: [1.5], 4: [2.0]}),
    ):
        (cache / name).mkdir(parents=True)
        (cache / name / "k_kernel.autotune.json").write_text(
            json.dumps(_autotune_record(key, timings))
        )
    configs = fla_configs.configs_from_triton_cache(cache, triton_version="3.8.0")
    document = configs["k_kernel"]
    assert document["default_config"]["num_warps"] == 2  # two of three tuned entries
    assert document["triton_version"] == "3.8.0"
    assert len(document["autotune_entries"]) == 3
    entry = document["autotune_entries"][fla_configs.key_hash([10, 32, False])]
    assert entry["config"]["num_warps"] == 2 and entry["autotune_key"] == [10, 32, False]
    out = tmp_path / "fla"
    assert fla_configs.write_configs(configs, out) == 1
    assert (out / "k_kernel.json").read_text() == (out / "k_kernel.json").read_text()
    assert json.loads((out / "k_kernel.json").read_text()) == document
    with pytest.raises(ValueError, match="no timed configs"):
        fla_configs.best_config({"configs_timings": []})
    with pytest.raises(ValueError, match="required fields"):
        fla_configs.best_config({"configs_timings": [[{"num_warps": 1}, [1.0]]]})


def test_fla_key_hash_matches_flas_scheme():
    import hashlib

    from citypods.gpu_launch import fla_configs

    # fla hashes json.dumps(key, separators=(",", ":"), sort_keys=True) with md5.
    expected = hashlib.md5(b'[1,"torch.float32",true]').hexdigest()  # noqa: S324
    assert fla_configs.key_hash([1, "torch.float32", True]) == expected


def test_timeout_uses_the_load_tail_while_cost_uses_the_median():
    steady = plan_launch(profile(L4=calib(load_seconds_p90=90.0)), 50_000, gpus=["L4"]).chosen
    tailed = plan_launch(profile(L4=calib(load_seconds_p90=300.0)), 50_000, gpus=["L4"]).chosen
    assert tailed.cost_microdollars == steady.cost_microdollars
    assert tailed.timeout_seconds > steady.timeout_seconds
    assert tailed.reservation_microdollars > steady.reservation_microdollars
    with pytest.raises(ValueError, match="at least load_seconds"):
        calib(load_seconds_p90=10.0)


def test_records_from_both_layouts_and_merge_into_a_profiles_file(tmp_path):
    probe = {
        "load_seconds": 80.0,
        "records": [
            {
                "token_budget": 8192,
                "peak_gpu_gib": 30.5,
                "micro_batches": [
                    {"padded_tokens": 8000, "cold_seconds": 5.0, "warm_seconds": 2.0},
                    {"padded_tokens": 8000, "cold_seconds": 2.2, "warm_seconds": 2.0},
                ],
            }
        ],
    }
    records = calibrate.records_from_result(probe, model_id="gemma", gpu="RTX_PRO_6000")
    cal = calibrate.calibrate(records, source="hf probe")
    assert cal.tokens_per_second == 4000.0 and cal.cold_overhead_seconds == 3.2
    wrapped = calibrate.records_from_result({"telemetry": telemetry()}, model_id="m", gpu="L4")
    assert wrapped[0]["micro_batches"][0]["padded_tokens"] == 16000
    path = tmp_path / "profiles.json"
    path.write_text(Path("config/gpu_profiles.json").read_text())
    with pytest.raises(ValueError, match="unknown model"):
        calibrate.merge_calibration(path, "gemma", "RTX_PRO_6000", cal)
    descriptor = {
        "family": "generic",
        "revision": "r",
        "weight_gib": 58.0,
        "max_len": 4096,
        "notes": "n",
    }
    calibrate.merge_calibration(path, "gemma", "RTX_PRO_6000", cal, new_model=descriptor)
    loaded = load_profiles(path)
    assert loaded["gemma"].calibration("RTX_PRO_6000") == cal
    assert "glide-9b" in loaded


def test_cli_plan_and_calibrate(tmp_path, capsys):
    from scripts import gpu_launch

    assert gpu_launch.main(["plan", "--model", "glide-9b", "--tokens", "53360"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["chosen"]["gpu"] == "L4"
    assert gpu_launch.main(["plan", "--model", "glide-9b", "--chars", "1000,2000"]) == 0
    capsys.readouterr()
    assert gpu_launch.main(["plan", "--model", "nope", "--tokens", "1"]) == 2
    assert gpu_launch.main(["plan", "--model", "glide-9b", "--tokens", "1", "--deadline", "1"]) == 1
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"telemetry": telemetry()}))
    profiles = tmp_path / "p.json"
    profiles.write_text(Path("config/gpu_profiles.json").read_text())
    argv = ["--profiles", str(profiles), "calibrate", "--model", "glide-9b", "--gpu", "L4"]
    assert gpu_launch.main([*argv, "--records", str(result), "--source", "test", "--write"]) == 0
    assert load_profiles(profiles)["glide-9b"].calibration("L4").source == "test"


def test_load_tail_uses_the_p90_once_there_are_five_samples():
    from citypods.gpu_launch.calibrate import _load_tail

    assert _load_tail([100.0]) == pytest.approx(130.0)
    assert (
        _load_tail([100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0, 400.0]) == 180.0
    )


def test_config_overhead_defaults_to_the_cold_overhead_until_measured():
    records = [calibrate.record_from_telemetry(telemetry(), model_id="m", gpu="L4")]
    assert calibrate.calibrate(records, source="s").config_overhead_seconds == 49.0
    measured = calibrate.calibrate(records, source="s", config_overhead=7.0)
    assert measured.config_overhead_seconds == 7.0 and measured.cold_overhead_seconds == 49.0


def test_records_can_be_limited_to_one_token_budget():
    batch = {"padded_tokens": 100, "cold_seconds": 2.0, "warm_seconds": 1.0}
    probe = {
        "load_seconds": 5.0,
        "records": [
            {"token_budget": 8192, "peak_gpu_gib": 1.0, "micro_batches": [batch]},
            {"token_budget": 4096, "peak_gpu_gib": 0.5, "micro_batches": [batch]},
        ],
    }
    kept = calibrate.records_from_result(probe, model_id="m", gpu="L4", token_budget=4096)
    assert [r["token_budget"] for r in kept] == [4096]
    assert len(calibrate.records_from_result(probe, model_id="m", gpu="L4")) == 2


def test_a_long_load_prefers_one_long_launch_over_repeating_the_load():
    heavy = profile(RTX_PRO_6000=calib(load_seconds=500.0, load_seconds_p90=650.0))
    one = plan_launch(heavy, 400_000, gpus=["RTX_PRO_6000"]).chosen
    capped = plan_launch(heavy, 400_000, gpus=["RTX_PRO_6000"], ceiling=900).chosen
    assert one.launches == 1 and capped.launches > 1
    assert one.cost_microdollars < capped.cost_microdollars
