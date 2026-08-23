import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel

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
    second = register_response_model("test-idempotent", DummyModelB)
    assert first is DummyModelA
    assert second is DummyModelA
    assert response_model("test-idempotent") is DummyModelA


def test_concurrent_registration():
    contract_name = "test-concurrent-race"
    barrier = threading.Barrier(10)

    def _worker(i: int):
        barrier.wait()

        class LocalModel(BaseModel):
            idx: int = i

        return register_response_model(contract_name, LocalModel)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_worker, i) for i in range(10)]
        results = [f.result() for f in futures]

    # All threads receive the same winning registered class object without exceptions
    assert all(r is results[0] for r in results)
    assert response_model(contract_name) is results[0]
