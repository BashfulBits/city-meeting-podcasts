"""Unit tests for the SSRF/abuse gate (audit #S1)."""

from __future__ import annotations

import pytest

from citypods.security import (
    SecurityError,
    allowed_hosts_for_city,
    iter_source_urls,
    redact_subprocess_command,
    redact_subprocess_text,
    validate_city_sources,
    validate_source_url,
)

GRANICUS = ("*.granicus.com",)


def test_subprocess_redaction_strips_signed_queries_and_bearer_values():
    signed = "https://media.example/video.mp4?X-Amz-Credential=secret&X-Amz-Signature=also-secret"
    text = f"{signed}: Server returned 403; token=plain-secret Authorization: Bearer bearer-secret"

    redacted = redact_subprocess_text(text)
    command = redact_subprocess_command(
        ["ffmpeg", "-headers", "Authorization: Bearer bearer-secret\r\n", "-i", signed]
    )

    assert redacted == (
        "https://media.example/video.mp4: Server returned 403; token=<redacted> "
        "Authorization: Bearer <redacted>"
    )
    rendered_command = " ".join(command)
    assert "media.example/video.mp4" in rendered_command
    assert "secret" not in rendered_command
    assert "Bearer <redacted>" in rendered_command


def _resolver(ip):
    return lambda host: [ip]


# --- validate_source_url: scheme -------------------------------------------------------


def test_rejects_non_https_scheme():
    with pytest.raises(SecurityError, match="https only"):
        validate_source_url("http://foo.granicus.com/x", allowed_hosts=GRANICUS, resolve=False)


def test_rejects_file_scheme():
    with pytest.raises(SecurityError, match="https only"):
        validate_source_url("file:///etc/passwd", resolve=False)


def test_rejects_schemeless():
    with pytest.raises(SecurityError, match="no scheme"):
        validate_source_url("foo.granicus.com/x", resolve=False)


# --- validate_source_url: host allowlist -----------------------------------------------


def test_disallowed_host_rejected():
    with pytest.raises(SecurityError, match="not in the allowlist"):
        validate_source_url("https://evil.example.com/x", allowed_hosts=GRANICUS, resolve=False)


def test_lookalike_apex_host_rejected():
    # ``*.granicus.com`` must not match a look-alike apex domain.
    with pytest.raises(SecurityError, match="not in the allowlist"):
        validate_source_url("https://evilgranicus.com/x", allowed_hosts=GRANICUS, resolve=False)


def test_allowed_host_passes():
    # Allowed host + a resolver returning a public IP -> no raise.
    validate_source_url(
        "https://denton.granicus.com/ViewPublisherRSS.php?view_id=2",
        allowed_hosts=GRANICUS,
        resolve=True,
        resolver=_resolver("8.8.8.8"),
    )


def test_allowed_host_subdomain_passes():
    validate_source_url("https://a.b.granicus.com/x", allowed_hosts=GRANICUS, resolve=False)


# --- validate_source_url: private/loopback/link-local IPs ------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # cloud metadata (link-local)
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 unique-local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
    ],
)
def test_blocked_private_ip(ip):
    with pytest.raises(SecurityError, match="non-public address"):
        validate_source_url(
            "https://denton.granicus.com/x",
            allowed_hosts=GRANICUS,
            resolve=True,
            resolver=_resolver(ip),
        )


def test_public_ip_allowed():
    validate_source_url(
        "https://denton.granicus.com/x", resolve=True, resolver=_resolver("8.8.8.8")
    )


def test_mixed_resolution_rejected_if_any_private():
    # A host that resolves to both a public and an internal address must be rejected.
    def resolver(host):
        return ["8.8.8.8", "169.254.169.254"]

    with pytest.raises(SecurityError, match="non-public address"):
        validate_source_url("https://denton.granicus.com/x", resolve=True, resolver=resolver)


def test_unresolvable_host_rejected(monkeypatch):
    # The default resolver turns a DNS failure into a SecurityError (refuse, don't fetch).
    import socket

    def boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(SecurityError, match="could not resolve"):
        validate_source_url("https://nope.granicus.com/x", resolve=True)


# --- per-provider allowlist derivation -------------------------------------------------


def test_allowed_hosts_for_known_providers():
    assert allowed_hosts_for_city("granicus", None) == ("*.granicus.com",)
    assert "*.swagit.com" in allowed_hosts_for_city("swagit", None)
    assert "*.api.civicclerk.com" in allowed_hosts_for_city("civicclerk", None)


def test_civicplus_allows_city_own_domain():
    hosts = allowed_hosts_for_city("civicplus", "https://www.gainesville.tx.us")
    assert "www.gainesville.tx.us" in hosts
    assert "*.gainesville.tx.us" in hosts
    # the city's CivicMedia feed on its own domain validates...
    validate_city_sources(
        "civicplus",
        {"feed_url": "https://www.gainesville.tx.us/RSSFeed.aspx?ModID=92&CID=City-Council"},
        "https://www.gainesville.tx.us",
    )
    # ...but a feed_url on a foreign domain does not.
    with pytest.raises(SecurityError, match="not in the allowlist"):
        validate_city_sources(
            "civicplus",
            {"feed_url": "https://evil.example.com/RSSFeed.aspx"},
            "https://www.gainesville.tx.us",
        )


# --- iter_source_urls / validate_city_sources ------------------------------------------


def test_iter_source_urls_handles_lists_and_skips_non_urls():
    source = {
        "feed_urls": ["https://a.granicus.com/1", "https://b.granicus.com/2"],
        "body": "City Council",
        "category_id": 26,
    }
    assert sorted(iter_source_urls(source)) == [
        "https://a.granicus.com/1",
        "https://b.granicus.com/2",
    ]


def test_validate_city_sources_checks_every_feed_url():
    good = {"feed_urls": ["https://a.granicus.com/1", "https://b.granicus.com/2"]}
    validate_city_sources("granicus", good)  # no raise
    bad = {"feed_urls": ["https://a.granicus.com/1", "https://evil.example.com/2"]}
    with pytest.raises(SecurityError, match="not in the allowlist"):
        validate_city_sources("granicus", bad)


def test_validate_city_sources_passes_each_maintainer_provider():
    validate_city_sources("granicus", {"feed_url": "https://denton.granicus.com/x"})
    validate_city_sources("swagit", {"list_url": "https://dallastx.new.swagit.com/views/5"})
    validate_city_sources("civicclerk", {"api_base": "https://traviscotx.api.civicclerk.com"})
