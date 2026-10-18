- **Fix October 18 workflow failures.** Remedy feedback installs its policy-model dependencies
  in both feedback jobs and the disposition/reopen commands. Stuck-job reconciliation accepts
  zero as an explicit no-minimum-age threshold, retaining lane, in-flight lease, and write-budget
  guards. Internal and external ASR workers requeue transient object-read outages as deferred
  instead of marking the claim failed. No pipeline-version changes, artifact invalidation, or
  catalog backfill.
