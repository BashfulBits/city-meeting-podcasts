"""Tests for the R2 endpoint fallback used by maintenance scripts."""

from __future__ import annotations

import boto3
import pytest

from scripts import reindex_llm_dispatch_queue, requeue_failed_llm_dispatch, spike_r2_cas


@pytest.mark.parametrize(
    ("factory", "access_key", "secret_key"),
    [
        (spike_r2_cas._make_client, "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"),
        (reindex_llm_dispatch_queue._client, "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"),
        (
            requeue_failed_llm_dispatch._client,
            "R2_RECLAIM_ACCESS_KEY",
            "R2_RECLAIM_SECRET_ACCESS_KEY",
        ),
    ],
)
def test_r2_script_clients_use_account_default_for_blank_endpoint(
    monkeypatch, factory, access_key, secret_key
):
    captured = {}

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: captured.update(kwargs) or kwargs)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv(access_key, "key")
    monkeypatch.setenv(secret_key, "secret")
    monkeypatch.setenv("R2_ENDPOINT", " ")

    factory()

    assert captured["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"


def test_r2_script_client_preserves_explicit_jurisdiction_endpoint(monkeypatch):
    captured = {}

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: captured.update(kwargs) or kwargs)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.eu.r2.cloudflarestorage.com")

    spike_r2_cas._make_client()

    assert captured["endpoint_url"] == "https://acct.eu.r2.cloudflarestorage.com"
