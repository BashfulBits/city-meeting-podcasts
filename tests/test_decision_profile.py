"""Offline checks of the decision-batch inference diagnostics."""

import logging

import pytest

from citypods.judging import decision_profile as dp
from tests.test_decision_batch_worker import RequestError, make_row, worker, write_input

FALLBACK = "`{}` is falling back to its reference PyTorch implementation because `fla` is missing."


class Shape:
    def __init__(self, rows, width):
        self.shape = (rows, width)


def test_pack_microbatches_mirrors_length_sorted_greedy_packing():
    # Ascending [100, 100, 100, 100, 300]: four rows fit 400, the 300 starts a second batch.
    assert dp.pack_microbatches([300, 100, 100, 100, 100], 400) == (2, 400 + 300)
    assert dp.pack_microbatches([], 400) == (0, 0)
    # A prompt longer than the budget still gets its own micro-batch.
    assert dp.pack_microbatches([500], 400) == (1, 500)


def test_token_summary_reports_distribution_and_padding():
    summary = dp.token_summary([100, 100, 100, 100, 300], 400)
    assert summary["count"] == 5
    assert summary["sum"] == 700
    assert (summary["min"], summary["median"], summary["max"]) == (100, 100, 300)
    assert summary["expected_microbatches"] == 2
    assert summary["expected_padded_tokens"] == 700
    assert summary["padding_overhead"] == 0.0
    assert dp.token_summary([], 400) == {"count": 0}
    assert "token_budget" not in dp.token_summary([5], None)


def test_fallback_watch_records_only_unique_matching_warnings():
    watch = dp.FallbackWatch().install()
    logger = logging.getLogger("transformers.integrations.hub_kernels")
    try:
        logger.warning(FALLBACK.format("causal_conv1d_fn"))
        logger.warning(FALLBACK.format("causal_conv1d_fn"))
        logger.warning("unrelated warning")
        assert watch.fallbacks == ["causal_conv1d_fn"]
        watch.assert_fast()  # the convolution fallback is recorded, not fatal
        logger.warning(FALLBACK.format("chunk_gated_delta_rule"))
        with pytest.raises(RuntimeError, match="chunk_gated_delta_rule"):
            watch.assert_fast()
    finally:
        watch.uninstall()
    logger.warning(FALLBACK.format("fused_recurrent_gated_delta_rule"))
    assert "fused_recurrent_gated_delta_rule" not in watch.fallbacks


class Network:
    def __init__(self, on_forward=lambda: None):
        self.on_forward = on_forward
        self.count = 0

    def forward(self, x, mask):
        self.count += 1
        self.on_forward()
        return f"out{self.count}"


def test_forward_timer_times_each_call_and_restores_forward():
    ticks = iter(range(0, 1000, 2))
    network = Network()
    original = network.forward
    timer = dp.ForwardTimer(network, clock=lambda: float(next(ticks)))
    network.forward(Shape(4, 100), None)
    network.forward(Shape(2, 300), None)
    assert [c["padded_tokens"] for c in timer.calls] == [400, 600]
    assert [c["seconds"] for c in timer.calls] == [2.0, 2.0]
    summary = timer.summary()
    assert summary["forward_calls"] == 2
    assert summary["padded_tokens"] == 1000
    assert summary["padded_tokens_per_second"] == 250.0
    assert "warm_repeat_warm_seconds" not in summary
    timer.detach()
    assert network.forward == original


def test_forward_timer_records_cpu_and_child_cpu():
    cpu = iter([(1.0, 0.0), (3.5, 6.0)])
    network = Network()
    timer = dp.ForwardTimer(network, cpu_times=lambda: next(cpu))
    network.forward(Shape(1, 10), None)
    assert timer.calls[0]["cpu_seconds"] == 2.5
    assert timer.calls[0]["child_cpu_seconds"] == 6.0


def test_forward_timer_warm_repeat_runs_first_calls_twice_and_returns_warm_output():
    ticks = iter(range(0, 1000, 5))
    network = Network()
    timer = dp.ForwardTimer(network, warm_repeats=1, clock=lambda: float(next(ticks)))
    assert network.forward(Shape(1, 10), None) == "out2"
    assert network.forward(Shape(1, 10), None) == "out3"
    assert network.count == 3  # only the first call repeats
    assert timer.calls[0]["warm_seconds"] == 5.0
    assert "warm_seconds" not in timer.calls[1]
    summary = timer.summary()
    assert summary["warm_repeat_cold_seconds"] == 5.0
    assert summary["warm_repeat_warm_seconds"] == 5.0


def test_profile_aborts_after_first_micro_batch_on_required_kernel_fallback():
    logger = logging.getLogger("transformers.integrations.hub_kernels")
    network = Network(lambda: logger.warning(FALLBACK.format("chunk_gated_delta_rule")))

    class Model:
        token_budget = 16384

    model = Model()
    model.network = network
    watch = dp.FallbackWatch().install()
    try:
        dp.InferenceProfile(model, watch)
        with pytest.raises(RuntimeError, match="fell back"):
            network.forward(Shape(1, 10), None)
        assert network.count == 1
    finally:
        watch.uninstall()


def test_worker_publishes_profile_in_telemetry(tmp_path):
    row = make_row({"type": "noul", "instructions": "yes?"})
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    write_input(source, [row])

    class Model:
        token_budget = 16384

        def __init__(self):
            self.network = Network()

        def decide_batch(self, requests):
            self.network.forward(Shape(1, 25), None)
            return [
                {
                    "answers": {row["item_id"]: {"type": "noul", "noul": 0.5}},
                    "usage": {"input_tokens": 25},
                }
            ]

    telemetry = worker.run_batch(source, output, lambda: (Model(), RequestError))
    profile = telemetry["profile"]
    assert profile["tokens"]["sum"] == 25
    assert profile["tokens"]["expected_microbatches"] == 1
    assert profile["forward"]["forward_calls"] == 1
    assert profile["kernel_fallbacks"] == []
    assert set(profile["runtime"]) >= {"torch", "triton", "flash-linear-attention"}


def test_worker_fails_when_fast_kernels_are_missing(tmp_path):
    row = make_row({"type": "noul", "instructions": "yes?"})
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    write_input(source, [row])
    logger = logging.getLogger("transformers.integrations.hub_kernels")

    class Model:
        def __init__(self):
            self.network = Network(
                lambda: logger.warning(FALLBACK.format("chunk_gated_delta_rule"))
            )

        def decide_batch(self, requests):
            self.network.forward(Shape(1, 25), None)
            raise AssertionError("inference continued past the failed kernel check")

    with pytest.raises(RuntimeError, match="chunk_gated_delta_rule"):
        worker.run_batch(source, output, lambda: (Model(), RequestError))
    assert not output.exists()


def test_warm_repeat_setting_is_validated(tmp_path, monkeypatch):
    row = make_row({"type": "noul", "instructions": "yes?"})
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    write_input(source, [row])
    monkeypatch.setenv(worker.WARM_REPEATS_ENV, "17")
    with pytest.raises(ValueError, match="integer from 0 through 16"):
        worker.run_batch(source, output, lambda: pytest.fail("model loaded"))
