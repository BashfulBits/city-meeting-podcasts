import hashlib
import types
from pathlib import Path

import pytest

from citypods.judging import open_weight_runtimes as rt
from tests._lazy import Lazy, require

torch = Lazy("torch")
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def need_torch():
    require("torch")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_verify_sources_accepts_pinned_bytes_and_refuses_anything_else(tmp_path, monkeypatch):
    (tmp_path / "engine.py").write_bytes(b"audited")
    monkeypatch.setitem(rt.SOURCE_SHA256, "elephant", {"engine.py": sha(b"audited")})
    rt.verify_sources("elephant", {"elephant": str(tmp_path)})
    (tmp_path / "engine.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs from the audited file"):
        rt.verify_sources("elephant", {"elephant": str(tmp_path)})
    (tmp_path / "engine.py").unlink()
    with pytest.raises(ValueError, match="differs"):
        rt.verify_sources("elephant", {"elephant": str(tmp_path)})


def test_pinned_source_tables_match_the_dockerfile():
    docker = (
        Path(__file__).parents[1] / "workers/decision-batch/open_weight_runtimes.Dockerfile"
    ).read_text()
    for files in rt.SOURCE_SHA256.values():
        for digest in files.values():
            # patched OneJev files are checked by the build's own import step, not downloaded
            assert len(digest) == 64
    for digest in (
        "72373353379c53b7cabfb5d05b6fef3c898d48c87cc386e03fc3de9c07e67ca7",
        "1717f334094b44926a4a46e7860d3e67135cc91158125f8cccf4a637ef757a32",
        "dc641d4dbac05cd088de59c20c06cfc44e1dcc18d4102470fc8feba943359639",
    ):
        assert digest in docker
    assert rt.ELEPHANT_REVISION in docker and rt.COPERNICUS_REVISION in docker
    assert "81ce62f1597c91e46767d4d02d6ac2e18534fe94" in docker
    assert "pip install" not in docker  # nothing is installed from the network


def test_compare_answers_reports_differences_flips_and_status_mismatches():
    def reply(*ps):
        return {"answers": {f"q{i}": {"type": "noul", "noul": p} for i, p in enumerate(ps)}}

    got = rt.compare_answers(
        [reply(0.2, 0.9), "too_large", reply(0.49)], [reply(0.25, 0.9), reply(0.5), reply(0.51)]
    )
    assert got["questions"] == 3 and got["flips_at_0_5"] == 1 and got["status_mismatches"] == 1
    assert got["max_abs_diff"] == pytest.approx(0.05)
    with pytest.raises(ValueError, match="different question sets"):
        rt.compare_answers([reply(0.1)], [reply(0.1, 0.2)])


class FakeUsage:
    input_tokens = 123


class FakeResponse:
    usage = FakeUsage()


class Unsupported(ValueError):
    pass


class FakeValidation(ValueError):
    pass


class FakeEngine:
    def __init__(self):
        self.calls = 0

    def decide(self, request):
        self.calls += 1
        return FakeResponse(), None


class FakeDecisionModel:
    def __init__(self, behaviour):
        self.engine, self.behaviour = FakeEngine(), behaviour

    def __call__(self, state, questions):
        self.engine.decide(None)
        outcome = self.behaviour(state)
        if isinstance(outcome, Exception):
            raise outcome
        return {"answers": {q: {"type": "noul", "noul": outcome} for q in questions}}, None


def test_elephant_runner_returns_answers_tokens_and_statuses():
    def behaviour(state):
        return {"long": Unsupported("limit is 5"), "bad": FakeValidation("x")}.get(state, 0.75)

    runner = rt.ElephantRunner(FakeDecisionModel(behaviour), Unsupported, FakeValidation)
    requests = [
        {"state": "ok", "questions": {"a": {}, "b": {}}},
        {"state": "long", "questions": {"a": {}}},
        {"state": "bad", "questions": {"a": {}}},
    ]
    ok, long, bad = runner.run(requests)
    assert ok == {
        "answers": {"a": {"type": "noul", "noul": 0.75}, "b": {"type": "noul", "noul": 0.75}},
        "usage": {"input_tokens": 123},
    }
    assert (long, bad) == ("too_large", "invalid_input")


def test_runner_maps_cuda_out_of_memory_to_too_large(need_torch):
    def behaviour(state):
        return torch.OutOfMemoryError("oom")

    runner = rt.ElephantRunner(FakeDecisionModel(behaviour), Unsupported, FakeValidation)
    assert runner.run([{"state": "s", "questions": {"a": {}}}]) == ["too_large"]


def test_copernicus_runner_uses_the_engine_run_and_maps_errors():
    class Engine:
        def run(self, state, questions):
            if state == "long":
                raise Unsupported("too long")
            if state == "bad":
                raise ValueError("criteria")
            return {q: {"type": "noul", "noul": 0.1} for q in questions}, {"input_tokens": 9}

    runner = rt.CopernicusRunner(Engine(), Unsupported)
    ok, long, bad = runner.run(
        [{"state": s, "questions": {"q": {}}} for s in ("fine", "long", "bad")]
    )
    assert ok == {"answers": {"q": {"type": "noul", "noul": 0.1}}, "usage": {"input_tokens": 9}}
    assert (long, bad) == ("too_large", "invalid_input")


def test_loaders_refuse_a_manifest_for_another_revision():
    with pytest.raises(ValueError, match="pinned elephant-1"):
        rt.load_elephant({"revision": "0" * 40}, "token")
    with pytest.raises(ValueError, match="pinned Copernicus"):
        rt.load_copernicus({"revision": "0" * 40}, "token")


def test_copernicus_engine_uses_the_server_default_flags():
    seen = {}

    class Server:
        class Calibration:
            pass

        @staticmethod
        def Copernicus(*args):  # noqa: N802
            seen["args"] = args
            return "engine"

    layout = types.SimpleNamespace(Layout=lambda **kw: kw)
    assert rt._copernicus_engine(Server, layout, "tok", "backend") == "engine"
    tok, backend, temperature, workers, temps, lay, _, urls, prompt, template, coords = seen["args"]
    assert (tok, backend, temperature, workers, temps) == ("tok", "backend", 1.0, 16, {})
    assert lay == {
        "header": False,
        "header_min_questions": 1,
        "read_twice": True,
        "twice_max_tokens": 3000,
        "referenced_fields": False,
    }
    assert (urls, prompt, template, coords) == (False, "locate", None, None)


def test_streamed_backend_is_the_authors_class_around_an_existing_model(need_torch):
    class HF:
        def score(self, text, media, ids):
            return self.model, self.tok

    server = types.SimpleNamespace(HF=HF)
    backend = rt._streamed_hf(server, torch, "model", "tok")
    assert isinstance(backend, HF) and backend.score("t", None, []) == ("model", "tok")
    assert backend.torch is torch and backend.lock is not None


QWEN = FIXTURES / "karpo_qwen3_5_tiny_config.json"


def test_qwen_causal_wrapper_has_the_logits_of_the_conditional_model(tmp_path, need_torch):
    qwen = pytest.importorskip("transformers.models.qwen3_5.modeling_qwen3_5")
    from transformers import AutoConfig

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(QWEN.read_text())
    config = AutoConfig.from_pretrained(config_dir)
    config.tie_word_embeddings = False
    config.get_text_config().tie_word_embeddings = False
    config.save_pretrained(config_dir)
    torch.manual_seed(0)
    full = qwen.Qwen3_5ForConditionalGeneration(config).to(torch.bfloat16).eval()
    with torch.no_grad():
        full.lm_head.weight.copy_(torch.randn_like(full.lm_head.weight))
    ids = torch.randint(0, config.get_text_config().vocab_size, (1, 11))
    with torch.no_grad():
        expected = full(input_ids=ids).logits
    causal = rt.causal_lm_from_backbone(
        full.model, {"lm_head.weight": full.lm_head.weight.detach()}, config_dir, torch
    )
    with torch.no_grad():
        got = causal(input_ids=ids).logits
    assert got.shape == expected.shape and torch.equal(got, expected)
    assert full.model.visual is None


def test_elephant_causal_wrapper_rejects_a_bad_head_and_a_tied_checkpoint(tmp_path, need_torch):
    qwen = pytest.importorskip("transformers.models.qwen3_5.modeling_qwen3_5")
    from transformers import AutoConfig

    (tmp_path / "config.json").write_text(QWEN.read_text())
    config = AutoConfig.from_pretrained(tmp_path)
    model = qwen.Qwen3_5ForConditionalGeneration(config).to(torch.bfloat16)
    with pytest.raises(ValueError, match="invalid lm_head"):
        rt.causal_lm_from_backbone(model.model, {}, tmp_path, torch)
    head = {"lm_head.weight": model.lm_head.weight.detach()}
    if config.get_text_config().tie_word_embeddings:
        with pytest.raises(ValueError, match="untied"):
            rt.causal_lm_from_backbone(model.model, head, tmp_path, torch)


def test_lean_head_logits_equal_the_float32_copy_the_engine_would_make(need_torch):
    torch.manual_seed(0)
    weight = torch.randn(50, 16, dtype=torch.bfloat16)
    hidden = torch.randn(3, 2, 16, dtype=torch.bfloat16)
    head = rt.lean_head_class(torch)(weight)
    old = rt.HEAD_CHUNK
    rt.HEAD_CHUNK = 7  # several chunks, one ragged
    try:
        got = head(hidden)
    finally:
        rt.HEAD_CHUNK = old
    reference = hidden.float() @ weight.float().t()
    assert got.dtype == torch.float32 and got.shape == (3, 2, 50)
    assert torch.allclose(got, reference, rtol=0, atol=1e-5)
    assert head.weight.dtype == torch.bfloat16 and not head.weight.requires_grad


def test_chunked_mlp_matches_the_unchunked_one(need_torch):
    torch.manual_seed(1)

    class Mlp(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.up = torch.nn.Linear(8, 16)
            self.down = torch.nn.Linear(16, 8)

        def forward(self, x):
            return self.down(torch.nn.functional.silu(self.up(x)))

    layers = [types.SimpleNamespace(mlp=Mlp()) for _ in range(2)]
    model = types.SimpleNamespace(model=types.SimpleNamespace(layers=layers))
    x = torch.randn(2, 11, 8)
    expected = [layer.mlp(x) for layer in layers]
    assert rt.chunk_mlps(model, 4) == 2
    for layer, want in zip(layers, expected, strict=True):
        got = layer.mlp(x)
        assert got.shape == want.shape and torch.allclose(got, want, atol=1e-6)
    short = torch.randn(1, 3, 8)
    assert layers[0].mlp(short).shape == (1, 3, 8)
