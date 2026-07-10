"""SSRF / abuse gate for source URLs (audit #S1, PLAN Change 9).

Today every ``source`` URL is maintainer-authored, so fetching only ever hits URLs we
wrote. Phase 5 onboards cities from GitHub-issue submissions, at which point the build —
which runs with repo-write + B2 secrets in env — would fetch attacker-influenced URLs.
This module is the trust boundary that makes that safe:

* ``validate_source_url`` — the primitive: https-only scheme, optional per-provider host
  allowlist, and (when ``resolve=True``) reject any host that resolves to a private,
  loopback, link-local, or otherwise non-public IP (blocks ``169.254.169.254`` &c.).
* ``validate_city_sources`` — config-load gate: scheme + host allowlist for every URL in a
  city's ``source`` (no DNS, so it stays offline/fast and runs on every config load).
* ``GuardedHTTPAdapter`` / :func:`citypods.http.make_session` — fetch-time gate: every
  outbound request (and every redirect hop) is re-checked *with* DNS resolution, redirects
  are capped, and oversized responses are refused.

The two layers are deliberately split: the host allowlist constrains the *configured*
source (the attacker-controlled input), while the universal IP guard protects every actual
fetch — including resolved/redirected media URLs that legitimately hop to non-allowlisted
public hosts (Swagit's presigned S3, CivicPlus's TikiLive embed).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable, Iterator
from fnmatch import fnmatch
from urllib.parse import urlsplit, urlunsplit

# https only — no http, file, ftp, gopher, data, ...
ALLOWED_SCHEMES = frozenset({"https"})

# How many redirect hops a fetch may follow before we give up (each hop is re-validated).
MAX_REDIRECTS = 5

# Refuse responses whose advertised size exceeds this. Sources here are feeds / JSON / HTML
# index pages (media bytes are streamed straight to ffmpeg, not through requests), so a
# response this large is hostile or broken, not legitimate.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # 64 MiB

# Per-provider host allowlist. Patterns are fnmatch globs matched against the hostname;
# ``*.granicus.com`` matches any subdomain but not the bare apex or a look-alike like
# ``evilgranicus.com``. CivicPlus is self-hosted per city, so its allowlist is derived from
# the city's own ``city_website`` domain at validation time (see ``allowed_hosts_for_city``).
PROVIDER_HOST_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "granicus": ("*.granicus.com",),
    "swagit": ("*.swagit.com", "*.granicus.com"),  # Swagit is Granicus-owned
    "civicclerk": ("*.api.civicclerk.com", "*.civicclerk.com"),
    "civicplus": (),  # filled per-city from city_website
}

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(\b(?:x-amz-[a-z0-9-]+|signature|credential|token|key)\s*[=:]\s*)"
    r"([^\s&,;\"']+)"
)
_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+")


class SecurityError(ValueError):
    """A source URL was rejected by the SSRF/abuse gate.

    A ``ValueError`` so a rejection at config load is consistent with the other
    config-validation failures. ``run._process_city`` also catches it explicitly so a URL
    blocked mid-fetch is handled as a per-city error and the build continues. (It is *not* a
    ``ProviderError`` on purpose: that would make ``security`` import the providers package
    and form an import cycle with ``citypods.http``.)
    """


class MediaSourceTooLargeError(SecurityError):
    """A source media URL preflight (issue #497) found — or ``-rw_timeout``-bounded streaming
    later confirmed — an advertised size over the configured media-specific ceiling.

    Deliberately **not** caught anywhere as a "download failed, fall back to streaming the URL
    directly" signal: that fallback is exactly what would defeat the guard. Callers must let this
    propagate to the same materialize-failure/backoff path as any other encode error.

    ``code`` follows the same convention as ``ProviderError`` subclasses (e.g.
    ``RateLimitedMediaFetchError.code``) so ``record_materialize_failure``'s
    ``getattr(exc, "code", None)`` picks it up without a special case, and the feed-health audit
    (``citypods.audit.check_media_too_large``) can find every rejection by this stable string —
    rare enough that it should surface as a visible, human-reviewed ticket rather than silently
    joining the generic retry/backoff noise.
    """

    code = "media-too-large"


def redact_subprocess_text(value: bytes | str | None) -> bytes | str | None:
    """Redact credentials and URL queries from subprocess diagnostics.

    Provider media URLs often carry short-lived signed query strings. ffmpeg/ffprobe echo inputs
    in stderr, while ``CalledProcessError`` also renders command arguments. Preserve the useful
    scheme/host/path/status context but never retain a URL query, bearer value, or standalone
    credential-shaped assignment.
    """
    if value is None:
        return None
    was_bytes = isinstance(value, bytes)
    text = value.decode("utf-8", errors="replace") if was_bytes else str(value)

    def _strip_query(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,:);]":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return match.group(0)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) + trailing

    text = _URL_IN_TEXT_RE.sub(_strip_query, text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _CREDENTIAL_VALUE_RE.sub(r"\1<redacted>", text)
    return text.encode("utf-8") if was_bytes else text


def redact_subprocess_command(command: object) -> object:
    """Return a command safe to retain on a subprocess exception."""
    if isinstance(command, (list, tuple)):
        cleaned = [redact_subprocess_text(str(arg)) for arg in command]
        return tuple(cleaned) if isinstance(command, tuple) else cleaned
    return redact_subprocess_text(str(command))


def _hostname(url: str) -> str:
    # urlsplit lowercases nothing; normalize so allowlist/compare is case-insensitive and
    # strips any ``user:pass@`` and ``:port``.
    return (urlsplit(url).hostname or "").lower()


def _host_allowed(host: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch(host, pat.lower()) for pat in patterns)


# RFC 6598 shared address space (CGNAT, and some cloud providers' internal routing). Verified
# live that ipaddress flags every other check here False for e.g. 100.64.0.1 — is_private,
# is_reserved, etc. all miss this range, so it must be blocked explicitly (CR2-CP-47/H1).
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that must never be fetched from a build runner."""
    # IPv4-mapped IPv6 (``::ffff:169.254.169.254``) — judge by the embedded v4 address.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_ADDRESS_SPACE)
    )


def _default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to its IP literals via the system resolver.

    Returns every A/AAAA answer so we reject a host that resolves to even one private IP
    (a DNS that returns both a public and an internal address shouldn't slip through).
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SecurityError(f"could not resolve host {host!r}: {exc}") from exc
    return sorted({info[4][0] for info in infos})


def validate_source_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    resolve: bool = True,
    resolver=_default_resolver,
) -> None:
    """Raise :class:`SecurityError` if ``url`` is unsafe to fetch.

    * Scheme must be in :data:`ALLOWED_SCHEMES` (https only).
    * If ``allowed_hosts`` is given, the host must match one of its globs.
    * If ``resolve`` is True, every IP the host resolves to must be public (rejects
      private / loopback / link-local / reserved ranges, e.g. cloud metadata).

    ``resolve`` is False at config load (offline, fast — scheme + host only) and True at
    fetch time (network context). ``resolver`` is injectable for offline unit tests.
    """
    if not _SCHEME_RE.match(url or ""):
        raise SecurityError(f"source URL has no scheme: {url!r}")
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SecurityError(f"scheme {scheme!r} not allowed (https only): {url!r}")

    host = _hostname(url)
    if not host:
        raise SecurityError(f"source URL has no host: {url!r}")

    if allowed_hosts is not None and not _host_allowed(host, allowed_hosts):
        raise SecurityError(f"host {host!r} not in the allowlist for this source: {url!r}")

    if resolve:
        for ip_str in resolver(host):
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                raise SecurityError(f"host {host!r} resolved to a bad address {ip_str!r}") from None
            if _is_blocked_ip(ip):
                raise SecurityError(
                    f"host {host!r} resolves to non-public address {ip_str} — refusing: {url!r}"
                )


def _city_domain_patterns(city_website: str | None) -> tuple[str, ...]:
    """Allowlist globs for a CivicPlus city's self-hosted CivicMedia: the ``city_website``
    host itself plus any subdomain of its registrable domain (``www.x.tx.us`` also allows
    ``media.x.tx.us``)."""
    host = _hostname(city_website or "")
    if not host:
        return ()
    apex = host[4:] if host.startswith("www.") else host
    return (host, apex, f"*.{apex}")


def allowed_hosts_for_city(provider: str, city_website: str | None) -> tuple[str, ...] | None:
    """Host-allowlist globs for a provider, or ``None`` when no allowlist is configured for
    it (an unknown/future provider — its URLs still get the scheme + fetch-time IP checks,
    just no host constraint). CivicPlus is derived from the city's own domain."""
    if provider == "civicplus":
        return PROVIDER_HOST_ALLOWLIST["civicplus"] + _city_domain_patterns(city_website)
    return PROVIDER_HOST_ALLOWLIST.get(provider)


def iter_source_urls(source: dict) -> Iterator[str]:
    """Yield every URL-looking string in a provider ``source`` dict (handles list values
    like Granicus ``feed_urls``). Non-URL config values (``body``, ``category_id``, …) are
    skipped; anything with a scheme is yielded so non-https schemes are caught downstream."""

    def _candidates(value) -> Iterator[str]:
        if isinstance(value, str):
            if _SCHEME_RE.match(value):
                yield value
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from _candidates(item)

    for value in source.values():
        yield from _candidates(value)


def validate_city_sources(provider: str, source: dict, city_website: str | None = None) -> None:
    """Config-load gate: scheme + per-provider host allowlist for every URL in ``source``.

    No DNS (stays offline and fast on every config load); the resolve/private-IP check runs
    at fetch time via :class:`GuardedHTTPAdapter`.
    """
    allowed = allowed_hosts_for_city(provider, city_website)
    for url in iter_source_urls(source):
        validate_source_url(url, allowed_hosts=allowed, resolve=False)
