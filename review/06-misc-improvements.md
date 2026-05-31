# Misc codebase improvements

Smaller, high-confidence cleanups found while reading. Each is low-risk.

## Correctness / robustness
1. **Scripts import smoke test** (catches B1-class breakage) — `tests/test_scripts_import.py` that
   imports every `scripts/*.py`. CI would have caught the `generate_board_cities` breakage.
2. **Fetch retry/backoff** in `http.make_session` via `urllib3.Retry` (3 tries, backoff, 429/5xx).
   Reduces false-positive feed-health issues. (Audit R1.)
3. **Response-size cap** for list/JSON fetches (stream + abort > N MB). (Audit R2.)
4. **`defusedxml`** for provider XML parsing. (Audit S3.)
5. **`-protocol_whitelist`** on the ffmpeg command. (Audit S2.)
6. **Alias uniqueness validation** in `load_city_configs`. (Audit B2.)

## Clarity / maintainability
7. **Provider capabilities** as an explicit set instead of scattered `hasattr(provider,
   "fetch_chapters")` / `fetch_view_counts` / `episode_links` checks. (Doc 02 Change 8.) Makes the
   directory + projection UI able to reason about coverage.
8. **`StageStats` cost fields** (`bytes_written`, `seconds_spent`, `backlog`) — additive; unlocks
   the resource model and run history. (Doc 02 Change 1.)
9. **Centralize the audio-size constant** — the `0.00045·kbps·D` GB/episode math appears implicitly
   in media (bitrate) and will appear in projection; expose `media.bytes_per_minute(kbps)` and reuse.
10. **`_feed_label` / `author_key` / `_author_key`** — there are three near-identical slugify/keying
    helpers (`artwork.author_key`, `records._author_key`, `config`/`slugify` in scripts). Consolidate
    into one `slugify`/`author_key` in a small `text.py` util. Low risk, reduces drift.
11. **`render.py` docstring** says "feed and page templates"; now also redirect + (soon) admin/meeting
    pages — minor doc update.
12. **Type hints on `render_index`** — it's the one public function with untyped params
    (`cities, site_config, base_url, feed_info=None`). Add annotations.

## Tests
13. **Parser tests against real recorded fixtures** for the three new endpoints (doc 05 Layer 1).
14. **A `projection` golden test** pinning the §2 numbers in doc 03 so the model can't silently drift.
15. **JS/Python parity test** for the admin-page calculator (doc 03 §3e).

## Docs / ops
16. **`run_summary.json` + `run_history.jsonl`** (doc 02 Change 2) — also makes the preview workflow
    show "what this build would do".
17. **README/PLAN**: document the new stages (chapters, links), the `<podcast:chapters>` surface, the
    durable-state bucket model, and the new config knobs (`chapters_budget_per_run`,
    `run_time_budget_minutes`).
18. **`MEMORY.md`/architecture decision doc**: record the enrichment-stage + split-hash + bucket-state
    decisions as locked (they're load-bearing now). (I'll do this in phase 2.)

## Deliberately NOT changing
- The split-hash / content-addressed / uid design — sound, leave it.
- The single-JSON index — fine until a few hundred feeds; shard later (doc 02 Change 6), not now.
- The `DerivedArtifact` refactor (doc 02 Change 5) — wait for the third instance (transcripts).
- Broad `except Exception` in stages/audit — intentional resilience, keep (errors are recorded).
