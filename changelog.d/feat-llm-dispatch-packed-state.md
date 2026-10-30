- **LLM dispatch packed scheduler state (dormant).** The v2 coordinator can keep its compact
  queue, provider, route and estimate ledgers, active-lease registry and route-sharded usage and
  failure telemetry in packed form, cutting measured writes per cron tick by about 56% in replay.
  Legacy tables stay authoritative until an operator runs the guarded `POST /v2/packed-state`
  cutover under a drained global pause (rollback rebuilds the legacy tables). The coordinator also
  records recovery sweep statistics (and `RECOVERY_INTERVAL_MINUTES`, 0-1440, to space out
  expired-lease cleanup), at the cost of one scheduler-row write per idle tick. See review/57.
