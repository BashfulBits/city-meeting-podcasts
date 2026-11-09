Bound the judge's in-memory parsed transcript cache and spill older snapshots to private temporary
storage, preserving quote-check read limits and origin-refresh behavior. Report spill and rehydrate
counts to inform later tuning of the per-pass quote-check budget.
