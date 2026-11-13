- **Worker-cron producer trigger (review/58 WP6 part B).** The dispatch Worker's per-minute cron
  now starts the five LLM producer workflows (deferred sweep, chapter-agenda, chapter-locator, tag,
  judge) through GitHub `repository_dispatch`, because GitHub dropped most of their scheduled
  runs. The schedule is `config/producer_triggers.yml` (compiled to
  `src/producer_triggers.json`, drift-checked on deploy), matched statelessly against the tick's
  `scheduledTime` with no Durable Object access, 3 to 5 minutes before each workflow's GitHub
  cron minute and never on a cleanup minute. One request per due workflow, run one at a time
  under `ctx.waitUntil` with a 5 s timeout and at most 3 per tick, never awaited ahead of the
  claim; one `producer_trigger` log line per attempt (event type, status, duration, ok, and
  GitHub's `message` on failure; never the token). Inert until the owner sets the
  `GITHUB_DISPATCH_TOKEN` secret; `PRODUCER_TRIGGER_ENABLED=0` switches it off. Each workflow
  keeps its `schedule:` as a fallback; a schedule run skips itself (green) when a dispatched or
  manual run of the same workflow on the same branch succeeded and started within 45 min (sweep),
  90 min (agenda, locator, judge) or 180 min (tag) (`scripts/producer_schedule_guard.sh`,
  fail-open).
