# Migrations

## Stable episode identity (episode-record refactor)

**What changed:** the RSS `<guid>` for every episode switched from the provider's native id
(e.g. a Granicus GUID or Swagit video id) to a **stable, provider-independent uid** derived
from `author + meeting body + date` (see `citypods/records.py`).

**Why:** the provider id changes whenever a city migrates providers (we already moved Denton
from Granicus to Swagit), which makes podcast clients treat the entire back catalog as new and
re-download it. The stable uid survives provider migrations, so this churn happens **once** and
never again.

**Impact:** the first deploy after this change is a **one-time** event where existing
subscribers' clients may re-download recent episodes (clients key episodes by `<guid>`). This
was done deliberately during the beta period, before a meaningful subscriber base exists. After
this, guids are stable.

**Audio:** already-hosted audio is **not** re-encoded. `migrate_legacy_manifests` carries the
old per-slug `audio_manifest.json` entries over to the new record store by matching the
provider guid, marking them `spec_hash: "legacy"` (reused as-is until a real audio-spec change).

**State layout:** per-slug `audio_manifest.json` is superseded by a per-source record store at
`<state_dir>/sources/<source_key>/episodes.json` (restored across CI runs via `actions/cache`).
