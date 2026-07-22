"""Read-only provider-migration comparison and fail-closed cutover reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from citypods.models import City, Episode


@dataclass(frozen=True)
class MigrationItem:
    guid: str
    uid: str
    published: str
    title: str


@dataclass
class MigrationReport:
    slug: str
    source_id: str | None
    cutover: str
    mode: str
    archive_count: int
    candidate_count: int
    projected_count: int
    matched_history: list[MigrationItem] = field(default_factory=list)
    new_episodes: list[MigrationItem] = field(default_factory=list)
    ambiguous_history: list[MigrationItem] = field(default_factory=list)
    overrides_applied: list[MigrationItem] = field(default_factory=list)
    duplicate_uids: dict[str, list[str]] = field(default_factory=dict)
    invalid_overrides: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.source_id) and not (
            self.ambiguous_history or self.duplicate_uids or self.invalid_overrides
        )

    def to_dict(self) -> dict:
        value = asdict(self)
        value["ready"] = self.ready
        return value


def compare_provider_migration(
    city: City,
    candidate: list[Episode],
    archive: dict,
    *,
    cutover: date,
) -> MigrationReport:
    """Compare an already-UID-assigned candidate fetch with the append-only archive.

    An unmatched row dated before the declared cutover is deliberately ambiguous: it may be a
    renamed/shifted historical meeting and must be joined by a reviewed ``uid_overrides`` entry or
    explained by correcting the candidate config.  Archive rows absent from the candidate are not
    errors; that is the expected forward-only migration shape and append-only history retains them.
    """
    by_uid: dict[str, list[Episode]] = {}
    for episode in candidate:
        if not episode.uid:
            raise ValueError(f"{city.slug}: candidate episode {episode.guid!r} has no stable UID")
        by_uid.setdefault(episode.uid, []).append(episode)

    duplicate_uids = {
        uid: sorted({episode.guid for episode in episodes})
        for uid, episodes in by_uid.items()
        if len({episode.guid for episode in episodes}) > 1
    }
    invalid_overrides = [
        f"{guid}: target {uid} is absent from the existing archive"
        for guid, uid in sorted(city.uid_overrides.items())
        if uid not in archive
    ]
    candidate_guids = {episode.guid for episode in candidate}
    invalid_overrides.extend(
        f"{guid}: provider GUID is absent from the candidate fetch"
        for guid in sorted(city.uid_overrides)
        if guid not in candidate_guids
    )

    report = MigrationReport(
        slug=city.slug,
        source_id=city.source_id,
        cutover=cutover.isoformat(),
        mode="copied-history" if any(uid in archive for uid in by_uid) else "forward-only",
        archive_count=len(archive),
        candidate_count=len(candidate),
        projected_count=len(set(archive) | set(by_uid)),
        duplicate_uids=duplicate_uids,
        invalid_overrides=invalid_overrides,
    )
    for episode in sorted(candidate, key=lambda item: (item.published, item.guid)):
        item = MigrationItem(
            guid=episode.guid,
            uid=episode.uid or "",
            published=episode.published.isoformat(),
            title=episode.title,
        )
        if episode.uid in archive:
            report.matched_history.append(item)
            if city.uid_overrides.get(episode.guid) == episode.uid:
                report.overrides_applied.append(item)
        elif episode.published.date() < cutover:
            report.ambiguous_history.append(item)
        else:
            report.new_episodes.append(item)
    return report
