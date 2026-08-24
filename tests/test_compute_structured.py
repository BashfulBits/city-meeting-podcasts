import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel

import citypods.compute.structured as structured
from citypods.chapter_titles import (
    AGENDA_ITEM_EXTRACTOR_CONTRACT,
    ensure_agenda_item_extractor_contract,
)
from citypods.compute.structured import register_response_model, response_model


class DummyModelA(BaseModel):
    value: str


class DummyModelB(BaseModel):
    value: int


def test_register_and_lookup_contract():
    model = register_response_model("test-contract-lookup", DummyModelA)
    assert model is DummyModelA
    assert response_model("test-contract-lookup") is DummyModelA


def test_register_empty_name_raises():
    with pytest.raises(ValueError, match="duplicate or empty"):
        register_response_model("", DummyModelA)


def test_lookup_unregistered_raises():
    with pytest.raises(ValueError, match="unknown structured-output contract"):
        response_model("unregistered-contract-xyz")


def test_idempotent_registration():
    first = register_response_model("test-idempotent", DummyModelA)
    second = register_response_model("test-idempotent", DummyModelA)
    assert first is DummyModelA
    assert second is DummyModelA
    assert response_model("test-idempotent") is DummyModelA


def test_incompatible_registration_raises():
    register_response_model("test-conflicting", DummyModelA)

    with pytest.raises(ValueError, match="conflicting structured-output contract"):
        register_response_model("test-conflicting", DummyModelB)


def test_concurrent_registration():
    contract_name = "test-concurrent-race"
    barrier = threading.Barrier(10)

    def _worker(i: int):
        barrier.wait()

        class LocalModel(BaseModel):
            value: int = 0

        return register_response_model(contract_name, LocalModel)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_worker, i) for i in range(10)]
        results = [f.result() for f in futures]

    # All threads receive the same winning registered class object without exceptions
    assert all(r is results[0] for r in results)
    assert response_model(contract_name) is results[0]


def test_agenda_item_extractor_contract_initializes_concurrently(monkeypatch):
    """The run-313 helper returns one registered model when its first calls overlap."""
    with structured._LOCK:
        monkeypatch.delitem(
            structured._RESPONSE_MODELS, AGENDA_ITEM_EXTRACTOR_CONTRACT, raising=False
        )
    monkeypatch.delattr(ensure_agenda_item_extractor_contract, "model", raising=False)

    workers = 8
    barrier = threading.Barrier(workers)
    original_register = structured.register_response_model

    def _register_after_all_workers_arrive(name, model):
        barrier.wait()
        return original_register(name, model)

    monkeypatch.setattr(structured, "register_response_model", _register_after_all_workers_arrive)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: ensure_agenda_item_extractor_contract(), range(workers)))

    assert all(model is results[0] for model in results)
    assert response_model(AGENDA_ITEM_EXTRACTOR_CONTRACT) is results[0]
