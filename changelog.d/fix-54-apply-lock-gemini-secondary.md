- **A queued `/apply` is no longer cancelled by unrelated comments, and the reconcile workflow
  gets the second Gemini key.** The provider-catalog commands workflow shared its writer lock
  with every issue and PR comment (and every skipped reconciliation completion) that merely
  started it; GitHub keeps one pending run per concurrency group, so a `/apply` queued behind a
  running reconcile was replaced and cancelled by the next such event, which is what happened to
  the 2026-11-22 apply. Only an authorised `/apply` on the catalog issue and the success of a
  scheduled run on main now take the lock; every other event gets a group of its own. The
  reconcile workflow also passes `GEMINI_API_KEY_SECONDARY`, so the ten Gemini routes on the
  second account are no longer skipped as having no API key.
