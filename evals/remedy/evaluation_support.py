"""Shared frozen-contract loading and control acknowledgments; no scheduled run state."""

import importlib.util
import sys
from pathlib import Path


def load_contract():
    path = Path(__file__).resolve().parents[2] / (
        "evals/remedy/results/body-selection-2026-10-13/body_selection.py"
    )
    spec = importlib.util.spec_from_file_location("frozen_chat_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def checked(value):
    if value.get("ok") is not True:
        raise RuntimeError("Worker control did not acknowledge operation")
    return value


def timeout_handler(_signal, _frame):
    raise TimeoutError("Approved 720-second provider response deadline")
