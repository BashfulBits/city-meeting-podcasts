- **Recover Gemma packets after Google TPM refusals.** Keep per-packet Google route exclusions
  separate from size evidence and preserve the transient retry allowance for remaining routes
  of the same model. Wait for temporarily unavailable overflow capacity; report exhausted route
  recovery explicitly. Existing pins, free/paid permissions, quotas, and dispatch limits still
  apply. Add a nullable coordinator field without rewriting payloads or invalidating stored
  artifacts. No pipeline-version bump or automatic revival of already-failed jobs.
