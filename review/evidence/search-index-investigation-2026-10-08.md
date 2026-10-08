# Search index investigation — 2026-10-08

## Observed deployment

Run #37728571153 succeeded on commit 73d9f7cf. Search job113155348789 restored
checkpoint37720810791-1, ran from04:53:18 to05:13:35UTC, then saved a new checkpoint.
The independently downloaded search-complete-site artifact contains19 source JSON files,
but no data/search/manifest.json, no search/index.html and no .search-cache.json.
Its meta.json reports search_shards=0. Live manifest remains404. Thus a successful job
is not proof of a complete search index. No storage credentials or provider requests were used
in this investigation; the artifact and job logs were read through authenticated GitHub access.

## Reproduced defect

citypods/search.py build_search_index holds each source’s documents in memory and only writes
its shard/cache after processing every record in that source. Its stop check returns None
mid-source before saving those documents. The wrapper saves completed-source progress but
cannot retain the in-memory partial source. A source taking longer than20minutes can restart
forever. A credential-free synthetic test used five records and stopped after two conversions.
Three consecutive runs with the same cache each processed u0,u1 and cached zero completed
sources. This confirms lack of within-source progress; actual production blocking source is
not exposed in available logs/artifacts and remains unconfirmed.

prior_publication returns “deferred: retaining complete prior site” even when its complete
checkpoint is absent/invalid. In that case it leaves the current render output untouched.
The workflow uploads it as search-complete-site and exits successfully, explaining how an
empty search site can accompany green deployment status. No partial index should be published.

## Recommended bounded follow-up — L2, implementation not yet approved

Preserve completed per-record documents in the private working checkpoint, invalidated by
source/config/archive/publication/cache-version changes; resume unfinished sources next run.
Keep public manifest/page publication atomic and retain a prior complete index on deferral.
Report completed/remaining sources, the source interrupted and completed/remaining records;
distinguish deferral with a prior index from deferral with no index. Keep the20minute ceiling,
existing storage read-only behavior and immutable raw records/audio. Exact private checkpoint
layout and invalidation contract require L3 specification before code. Regression tests must
prove repeated bounded runs eventually finish a single large source, config changes invalidate
partial work, and failures preserve the last complete publication.
