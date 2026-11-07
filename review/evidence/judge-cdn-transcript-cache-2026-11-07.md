# Judge transcript CDN and cache preflight — 2026-11-07

This read-only preflight checks whether public transcript URLs can serve judge transcript bytes
through Cloudflare and estimates the parsed `EpisodeTexts` retained by one cache entry. It does
not change the production read cap or measure a full judge run.

## Method

- Read the 23 VTT transcript URLs listed in
  `evals/chapter-locator/holdout-v1/manifest.json`, sequentially, with the repository's guarded
  HTTP session. No storage credentials or LLM calls were used.
- Parse each response with the same `parse_transcript_segments(..., "vtt")` function used by the
  judge, then estimate parsed-cache bytes with the new runner estimator. The estimator uses a deep
  `sys.getsizeof` walk, excludes shared tag definitions and counts repeated references more than
  once. This is a conservative Python-object estimate, not process RSS.
- Repeat the same 23 public GETs after the initial fetches to check whether Cloudflare serves the
  immutable transcript URLs from cache.

## Results

| Measure | Result |
|---|---:|
| Distinct transcripts | 23 |
| First-fetch `CF-Cache-Status` | 23 `MISS` |
| Repeat-fetch `CF-Cache-Status` | 23 `HIT` |
| Total raw VTT bytes | 4,910,484 bytes |
| Median raw VTT | 189,888 bytes |
| Parsed segments | 57,164 |
| Parsed-cache estimate, median | 1,053,627 bytes per transcript |
| Parsed-cache estimate, p95 | 2,021,037 bytes per transcript |
| Parsed-cache estimate, maximum | 3,448,624 bytes per transcript |

A straight-line estimate at the measured median would be about 402 MiB for 400 entries. This is
only a rough scale check: the sample is small and from the chapter-locator holdout set, not the
current judge queue; cache estimates do not include process-wide baseline or transient allocations.
The new per-run metrics must be observed on a normal judge run before selecting a higher cap.
