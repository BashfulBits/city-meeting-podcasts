# Isolated recovery canary — #2220

Maintainer approved isolated production-code tests plus ordinary-run evidence on 2026-10-10,
replacing the requirement to wait for every rare recovery case naturally in production.
Removal activation remains a separate reviewed change.

`harness.js` imports the real coordinator without modifying it. It has no provider execution,
production namespace binding, object-storage binding or scheduled trigger. Its only binding
is a new SQLite `RecoveryCheck` namespace; its only environment variable is a fresh random
`TEST_AUTH`. Every request requires that token and POST. Never bind it to a production namespace.
The existing `bench/rows-written` harness remains local-only and was not deployed.

The bounded fixture has 24 synthetic jobs with missing legacy `queue_models`, obsolete model
indexes and the historical secondary index. One job uses the unroutable sentinel; another has a
stale model index. Two jobs fit the current test catalog; one exceeds its context window and 21
have no remaining route. All begin with attempts=2, schema retries=1, transient retries=3.

Verified sequence on Cloudflare:

1. `/seed`: populate only this isolated namespace and request a fresh bounded rescue pass.
2. `/fail`: throw on the third mutation inside the real rescue transaction. Compare all persisted
   jobs, model indexes, scheduler and route rows before/after; require exact rollback.
3. `/page`: run the real rescue/accounting transaction; first page repairs two and fails eighteen.
4. `/reset`: call `ctx.abort()`; the request fails and the next `/read` returns a new instance UUID.
   Compare the exact persisted snapshot, including accounting and checkpoint, across this reset.
5. `/page`: resume, failing four remaining jobs. A further page repairs/fails zero jobs.
6. Require completion, exactly two indexed queued jobs, correct `unadmissible`/`route_retired`
   reasons and unchanged retry counters for every job.
7. Remove the class with a `deleted_classes` migration, delete this temporary Worker, and verify
   both its script and DO namespace are absent from Cloudflare listings.

The committed result records source identity, deployment version and assertions, without auth tokens
or production data. The isolated runner generated a unique `citypods-temp-rescue-*` name, used a
private temporary Wrangler config, and POSTed with `citypods-recovery-verification/1.0` User-Agent.
Future deployment/re-execution requires maintainer authorization and fresh test credentials.

This proves transaction-failure rollback and actual instance reset on Cloudflare, not exhaustion of
Cloudflare's real daily quota. Real quota exhaustion was intentionally not induced. The Python
structural-audit regression separately exercises nonzero failure count and consumed schema correction,
blocking unchanged/missing generations and allowing a new fitting generation. Ordinary-run evidence
and the revised acceptance decision are recorded in review/48.
