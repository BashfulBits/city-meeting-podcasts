# Per-Meeting Pages Implementation Notes

This PR implements the R1 per-meeting permalink feature described in
[review/13](13-per-meeting-pages-and-search.md).

Meeting pages are published at `docs/<feed-slug>/<episode-uid>/index.html`, with a separate
uncapped archive at `docs/<feed-slug>/archive/index.html`. City pages and RSS items link to the
stable meeting URL. Page hashes remain separate from the capped feed hash so archived meeting
metadata can refresh without rebuilding an unchanged feed window.

The page renderer includes media availability notices, official source links, chapter seeking,
source-time provider deep-links, client-side synced transcript loading, and a prefilled problem
report link. The implementation deliberately keeps archive pagination out of R1, matching the
breakout's decision to revisit it when archive page size becomes material.
