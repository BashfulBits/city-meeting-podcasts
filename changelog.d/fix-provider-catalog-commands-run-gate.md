- **Provider catalog commands: scheduled rate/removal jobs no longer reject their own trigger.**
  The `workflow_run` gate compared `workflow_run.name` to the workflow name, but scheduled
  reconciliation runs carry their `run-name` (e.g. `... (daily-due-only)`), so the first scheduled
  run failed with no explanation. The gate now matches the reconcile workflow file path, and the
  automatic jobs print the result comment before exiting non-zero.
