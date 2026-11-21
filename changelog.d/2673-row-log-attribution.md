- **Dispatch row-write attribution.** Concurrent coordinator RPCs now report their own committed
  SQL writes instead of combining them into another call's log. Rollbacks discard their diagnostic
  counts; the persisted daily budget is unchanged. The cause of the historical 6,006-row logging
  gap remains unconfirmed.
