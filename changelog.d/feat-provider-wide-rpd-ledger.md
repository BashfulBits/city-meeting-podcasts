- **Provider-wide daily request pool.** A provider whose free tier is one account-level daily limit,
  not a per-model one, can now declare `rpd` on the provider in `config/provider_limits.yml`; all
  of its routes then share that pool. OrcaRouter is the first: its free 800/day was modelled as
  800 per route, so DeepSeek, GLM and HY3 could together be planned for 2,400/day against a real
  800 (on 2026-11-09 all three answered the same 429 with the same Retry-After while HY3 had no
  recorded usage). The compiler validates the pool (positive whole number, no route `rpd` above it,
  scaled with `split_cap_multiplier`) and passes it to the Worker and to every Python route. The
  Worker derives the pool's usage by summing the routes' own daily counts (no new stored counter),
  gates dispatch pacing and capacity ranking on it, reports `provider_pools` and pool-capped
  `rpd_remaining` in `/v2/dispatch:pause-status`, and treats a daily-window 429 from a pooled
  provider as the pool being spent, standing every route down until the reset. The Python direct
  ledger keeps the same pool in its provider ledger, with release/settle refunds and rollover.
