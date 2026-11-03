### Changed
- Provider catalog: the daily run now measures found candidates for up to two hours (across
  providers, several per provider) instead of one session per run. Models AA scores at or below
  our weakest lane model are auto-ignored, and unfinished input searches (outage, stalled) are
  re-queued up to four times.

### Fixed
- Provider catalog: `/apply` `test again` no longer fails when no saved results exist; discovery
  evidence is saved after every session so a crashed run keeps it; repeated connection resets at
  one input size now step the size down.
