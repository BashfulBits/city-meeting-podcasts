- **The detailed stats snapshot is metered, and route capacity has a cheap view.** On 2026-10-13 a
  review/51 evaluation polling `/v2/stats?detail=1` before every request (about 80,000 rows read
  a call, 4.65 million in three hours) exhausted the Durable Object's 5,000,000 daily row-read
  allowance, so the dispatch Worker refused every call, including the judge lane's enqueue, until
  the 00:00 UTC reset. `/v2/stats?routes=1` now returns the route inventory at a cost that does not
  grow with retained jobs, the evaluation uses it, and `detail=1` serves a cached copy for 5 minutes
  and computes at most 24 fresh snapshots a UTC day (then the last copy, or HTTP 429).
