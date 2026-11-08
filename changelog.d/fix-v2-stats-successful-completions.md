- **Reconcile lane completions with successful completions in /v2/stats.** Distribute
  the coordinator's authoritative `completed_today` count across ingress lanes in proportion
  to lane throughput, ensuring lane completions reflect successful jobs rather than raw attempt/retry
  calls, aligning with the models and providers view metrics.
