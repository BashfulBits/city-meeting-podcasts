"""CivicEngage Archive Center agenda/minutes adapter.

CivicEngage's legacy ``Archive.aspx`` pages list document detail links as ``ADID`` query
parameters.  The detail URL itself serves the PDF, so the adapter retains that official URL
without downloading document bytes.  It is an auxiliary source for a recording provider.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import AgendaRecord, ChangeToken, Episode
from citypods.providers.base import ProviderError
from citypods.security import validate_source_url

_MONTH_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}-\d{1,2}-\d{4}\b")


class _ArchiveLinkParser(HTMLParser):
    """Collect archive document links and their visible titles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        href = dict(attrs).get("href") or ""
        if re.search(r"(?:[?&])ADID=\d+", href, re.IGNORECASE):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        title = re.sub(r"\s+", " ", " ".join(self._text)).strip()
        self.links.append((self._href, title))
        self._href = None
        self._text = []


def _parse_date(title: str) -> datetime | None:
    match = _MONTH_DATE_RE.search(title)
    if match:
        try:
            return datetime.strptime(match.group(0), "%B %d, %Y").replace(tzinfo=UTC)
        except ValueError:
            pass
    match = _NUMERIC_DATE_RE.search(title)
    if match:
        try:
            return datetime.strptime(match.group(0), "%m-%d-%Y").replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def parse_civicengage_archive(
    content: bytes, *, archive_url: str, kind: str, body: str
) -> list[AgendaRecord]:
    """Parse one CivicEngage archive page into dated agenda or minutes records."""
    if kind not in {"agenda", "minutes"}:
        raise ValueError("CivicEngage archive kind must be 'agenda' or 'minutes'")
    parser = _ArchiveLinkParser()
    parser.feed(content.decode("utf-8", errors="replace"))
    records: list[AgendaRecord] = []
    for href, title in parser.links:
        published = _parse_date(title)
        if published is None:
            continue
        records.append(
            AgendaRecord(
                body=body,
                title=title,
                published=published,
                links={kind: urljoin(archive_url, href)},
            )
        )
    return records


class CivicEngageProvider:
    name = "civicengage"
    capabilities: frozenset[str] = frozenset({"agenda"})

    def validate(self, source: dict) -> None:
        for key in ("agenda_url", "minutes_url", "body"):
            if not source.get(key):
                raise ValueError(f"civicengage source requires {key!r}")
        # Config validation must remain DNS-free; make_session and the fetch loop apply the
        # resolving SSRF check at request time.
        validate_source_url(str(source["agenda_url"]), resolve=False)
        validate_source_url(str(source["minutes_url"]), resolve=False)

    def detect_change(self, source: dict) -> ChangeToken | None:
        return None

    def fetch_episodes(self, source: dict) -> list[Episode]:
        """Auxiliary-only provider; CivicEngage archives do not provide recordings."""
        self.fetch_agenda_index(source)
        return []

    def fetch_agenda_index(self, source: dict) -> list[AgendaRecord]:
        self.validate(source)
        body = str(source["body"])
        records: dict[tuple[str, str], AgendaRecord] = {}
        with make_session() as session:
            for kind, key in (("agenda", "agenda_url"), ("minutes", "minutes_url")):
                url = str(source[key])
                validate_source_url(url, resolve=True)
                try:
                    response = session.get(url, timeout=DEFAULT_TIMEOUT)
                except requests.RequestException as exc:
                    raise ProviderError(f"GET {url} failed: {exc}") from exc
                if response.status_code >= 400:
                    raise ProviderError(
                        f"GET {url} returned {response.status_code}",
                        status_code=response.status_code,
                    )
                for record in parse_civicengage_archive(
                    response.content, archive_url=url, kind=kind, body=body
                ):
                    marker = (record.body, record.published.date().isoformat())
                    existing = records.get(marker)
                    if existing is None:
                        records[marker] = record
                    else:
                        for kind, link in record.links.items():
                            if link in existing.links.values():
                                continue
                            if kind not in existing.links:
                                existing.links[kind] = link
                                continue
                            suffix = 2
                            while f"{kind}_{suffix}" in existing.links:
                                suffix += 1
                            existing.links[f"{kind}_{suffix}"] = link
        return sorted(records.values(), key=lambda record: record.published)

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        return episode.video_url

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        return None
