- **Producer funnel telemetry and an `/admin/status` LLM lane table (review/58 WP1b).** Producer
  `llm_work` run events gain, per purpose and additively (old events stay valid):
  `first_seen_at` and `oldest_eligible_at` (run-local); `queue` (queued work split into waiting
  at the Worker, completed not collected, failed at the Worker, and `unobserved`, from poll
  responses a run actually saw); `admission` (admitted, replayed, deferred, rejected, prior
  pending, cached), which the flush's re-report as `queued` no longer erases; `jobs`; and
  `packets` (judge lanes: outstanding, submitted, run-cap counts; `remedy:p3`: findings,
  submitted, pending, written to the daily report because that workflow persists no run event).
  `/admin/status` shows one row per configured lane with its review/58 section 4 class
  (input-starved, producer-limited, dispatcher-limited, return-path-limited, budget-capped,
  caught up, or not measured) and a summary such as "waiting on agenda text" or "eligible
  backlog N, backlog seen for at least X h". Producers do not poll, so the
  waiting-versus-completed split reads "not measured" until a run observes a poll; slot
  productivity and failed-attempt share are dispatcher-side and also show "not measured". No
  Worker, Durable Object, `/v2/stats` or storage-listing reads were added, no pipeline version
  changed, and no stored artifact is invalidated.
