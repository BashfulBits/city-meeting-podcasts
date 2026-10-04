"""Tests for the spike-r2-cas.yml CLI's argument parsing."""

from __future__ import annotations

import argparse

import pytest

from scripts.spike_r2_cas import _whole_int


def test_whole_int_accepts_github_actions_decimal_formatted_number_input():
    """A GitHub Actions `workflow_dispatch` input declared `type: number` renders as a decimal-
    formatted string (e.g. "20.0") even for a plain integer value or default -- a bare `type=int`
    on --latency-iterations rejected that shape outright and failed this workflow's manual
    dispatch before it did anything."""
    assert _whole_int("20") == 20
    assert _whole_int("20.0") == 20


def test_whole_int_rejects_a_genuine_fraction():
    with pytest.raises(argparse.ArgumentTypeError):
        _whole_int("20.5")
