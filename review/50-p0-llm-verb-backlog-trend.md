# review/50 — P0: per-verb LLM backlog trend (read-only)

**Maturity: L3 dev-ready · authored 2026-09-30 · parent: [review/49](49-judge-consensus-admission.md) phase P0 ·
GitHub issues to be cut on merge (PR1, PR2 below)**

## Goal

Give every LLM verb (chapter-agenda, chapter-locator, tagger, prelabeler, moments) one comparable, trustworthy number:
**episodes of backlog, and whether it is shrinking, flat or growing**, with the cause split into *waiting for route capacity*,
*held by an ingress/per-run cap*, *blocked upstream* and *held by policy*. review/49 §5 uses this as the single signal for "capacity
constrained" in the route leagues and for re-sizing lane quotas. **P0 is read-only: it changes no producer, no Worker, no ledger.**

Non-goals: counters in producers, a new state file, the status-page row, quota changes, league logic (P6), a
tagger-specific "LLM completed" counter (see "Later").

## Why this is small: the signal already exists, durably

Every scoped lane run already writes one event file, `state/run_events/<ts>-<github_run_id>-<phase>-<lane>-<shard|source|scope>.json`
(`citypods/run.py::_record_run_history`, `RUN_EVENTS_DIR_NAME`). Each holds, per stage, `ran`, `reused`, `backlog` (this is `StageStats.skipped`,
documented in `citypods/stages.py` as "the remaining backlog for this stage") and `defer_reasons` (a stable token to count map). The lane is in
`lane`, the time in `ts`, completion in `outcome`. There were 4,182 such files in durable storage on 2026-09-30, starting 2026-06-15.

Do **not** read `run_history.jsonl`: scoped runs append to it from separate workflows and it is overwritten by bucket sync; the copy in storage
ends 2026-08-09. `run_events/` is per-run and append-only.

Real backlog values, last run of each day (from the committed fixture `tests/fixtures/backlog_trend/run_events_2026_09_22_30.json`, trimmed real events):

| Verb (lane) | Backlog, daily last value 2026-09-24..30 | What it shows |
|---|---|---|
| chapter-agenda | 4,480 → 3,566 → 2,823 → 1,625 (`llm-pending`) | draining at about 800/day |
| chapter-locator | 22 → 156 → 412 → 556 (mostly `llm-pending`, some `producer-cap`) | rising because the agenda lane now feeds it |
| tagger | 16–31 (`tag-llm-dispatch`, `tag-llm-oversized` blocked) | flat, small |
| prelabeler | 172 → 131 → 102 → 79 → 68 (`tag-prelabeler-dispatch`) | draining |
| prelabeler-shadow | 32 → 80 → 136 → 122 (all `*-shadow-no-quota`) | held by its own daily write budget |
| moments | 17 working, 1,210 `rollout-dispatch-cap` | the 1,210 is a policy hold (council rollout), not capacity |

Two lessons encoded below: a **growing backlog is not proof of a capacity problem** (the locator is growing because its supply grew), so
"constrained" also needs a **drain-time** test; and `rollout-dispatch-cap` must never count as capacity backlog.

## Design

### Module

New file `citypods/ops/backlog_trend.py` (pure functions plus `main()`, run as `python -m citypods.ops.backlog_trend`). It imports only the
standard library, `yaml` (config) and, in `main` only, `citypods.statesync` / `citypods.storage` for reading. No import of `citypods.stages` or
`citypods.run` (avoid cycles and accidental side effects).

```python
@dataclass(frozen=True)
class BacklogParams:
    window_days: int = 7          # calendar days ending at the newest event day
    min_points: int = 4           # distinct event days required for a trend
    drain_days_max: float = 14.0  # constrained only if backlog / daily throughput exceeds this
    deadband_floor: float = 1.0   # episodes/day
    deadband_rel: float = 0.02    # of the window mean

@dataclass(frozen=True)
class VerbSpec:
    name: str; lane: str; stage: str; throughput_reliable: bool

VERBS: dict[str, VerbSpec]            # table below
STAGE_TOKENS: dict[str, dict[str, tuple[str, str]]]   # stage -> defer token -> (verb, class)

def load_events(state_dir: Path, *, since: datetime) -> list[dict]: ...
def daily_points(events, verb: str, params, *, through: date | None = None) -> list[DayPoint]: ...
def analyze(events, verb: str, params) -> VerbReport: ...
def analyze_all(events, params) -> dict[str, VerbReport]: ...
def params_from_config(site_config: Mapping) -> BacklogParams: ...   # reads `llm_backlog:` if present
def render_markdown(reports: Mapping[str, VerbReport]) -> str: ...
def main(argv: list[str] | None = None) -> int: ...
```

### Verbs

| Verb | `lane` in the event | `stage` key | Throughput reliable |
|---|---|---|---|
| `chapter-agenda` | `chapter-agenda` | `chapter_agenda` | yes (`ran` = episodes produced) |
| `chapter-locator` | `chapter-locator` | `chapter_locator` | yes |
| `tagger` | `tag` | `tags` | no (`ran` counts rule-only episodes too) |
| `prelabeler` | `tag` | `tags` | no |
| `prelabeler-shadow` | `tag` | `tags` | no |
| `moments` | `moments` | `moments` | no |

Judge, adjudicator and any later verb are added by appending a `VerbSpec` and its tokens (P1/P3); nothing else changes.

### Defer-token ownership and classes

A stage may serve several verbs (the `tags` stage reports tagger and prelabeler tokens side by side). Each token belongs to exactly one verb and
one class. A verb's backlog counts **only its own tokens**; a token owned by another verb on the same stage is ignored; a token owned by nobody is
**unclassified** and is reported, never silently dropped.

| Class | Meaning | Counts toward working backlog |
|---|---|---|
| `queued` | handed to the Worker, waiting for a route | yes |
| `ingress_limited` | held by a per-run cap, purpose write budget, or quota | yes |
| `stopped` | run ended before the work (wall clock, stop signal, budget stop) | yes |
| `blocked` | waiting on an upstream artifact or a too-large payload | no (reported) |
| `policy_held` | held by design (rollout cap) | no (reported) |
| `errored` | the last attempt failed | no (reported) |

| Stage | Token | Verb | Class |
|---|---|---|---|
| `chapter_agenda` | `llm-pending` | chapter-agenda | queued |
| | `producer-cap` | chapter-agenda | ingress_limited |
| | `stop-signal` | chapter-agenda | stopped |
| | `missing-agenda-artifact`, `missing-agenda-artifact-despite-accepted-quality`, `agenda-artifact-key-present-but-unreadable` | chapter-agenda | blocked |
| `chapter_locator` | `llm-pending` | chapter-locator | queued |
| | `producer-cap` | chapter-locator | ingress_limited |
| | `stop-signal` | chapter-locator | stopped |
| | `agenda-not-complete`, `missing-timed-transcript` | chapter-locator | blocked |
| `tags` | `tag-llm-dispatch` | tagger | queued |
| | `tag-llm-no-quota` | tagger | ingress_limited |
| | `tag-llm-stop`, `tag-budget-stop` | tagger | stopped |
| | `tag-llm-oversized` | tagger | blocked |
| | `tag-prelabeler-dispatch` | prelabeler | queued |
| | `tag-prelabeler-no-quota` | prelabeler | ingress_limited |
| | `tag-prelabeler-stop` | prelabeler | stopped |
| | `tag-prelabeler-oversized` | prelabeler | blocked |
| | `tag-prelabeler-shadow-dispatch` | prelabeler-shadow | queued |
| | `tag-prelabeler-shadow-no-quota` | prelabeler-shadow | ingress_limited |
| | `tag-prelabeler-shadow-error` | prelabeler-shadow | errored |
| `moments` | `llm-pending` | moments | queued |
| | `llm-capacity` | moments | ingress_limited |
| | `stop` | moments | stopped |
| | `rollout-dispatch-cap` | moments | policy_held |
| | `llm-error` | moments | errored |

(`moment-judge` and `moment-admission` events exist with `judge-capacity`, `rollout-dispatch-cap`, `llm-pending` and `stop`; they are out of scope for P0 and arrive as verbs
with the judge work.) These tokens are the complete set seen in the 4,182 events plus the literals in `stages.py`; the tagger tokens are built dynamically in
`TagsStage` (phase names), so a scan of `.defer("…")` literals alone would miss them. That is why unknown tokens are reported at runtime instead of asserted in a test.

### Algorithm (exact)

For one verb, using events whose `lane == spec.lane`, `spec.stage in event["stages"]`, and `outcome != "interrupted"`:

1. **Daily point.** Group by UTC date of `ts`. The point for a date comes from the **last** event of that date (by `ts`). `backlog` = Σ over that event's
   `defer_reasons` tokens owned by this verb whose class is in {`queued`, `ingress_limited`, `stopped`}. Also record the per-class subtotals and the unclassified tokens.
   `ran_day` = Σ `stages[stage].ran` over **all** that date's events.
2. **Window.** The last `window_days` dates that have a point, ending at the newest date. Fewer than `min_points` dates gives `trend = "insufficient_data"` and no other judgement.
3. **Trend.** Ordinary least-squares slope of `backlog` against the 0-based index of the points (episodes/day). `mean` = mean of the window's backlogs.
   `threshold = max(deadband_floor, deadband_rel × mean)`. `growing` if slope ≥ threshold, `shrinking` if slope ≤ −threshold, else `flat`.
4. **Throughput and drain.** `throughput_per_day` = mean of `ran_day` over the window's dates. `drain_days = backlog_last / throughput_per_day`
   (`null` when throughput is 0).
5. **Constrained.**
   - throughput-reliable verbs: `backlog_last > 0 AND (drain_days is null OR drain_days > drain_days_max) AND trend != "shrinking"`;
   - others: `trend == "growing"` (their `ran` overstates LLM completions, so drain is reported but not used).
6. **Action** (only when constrained): `raise_ingress_quota` if `ingress_limited / backlog_last ≥ 0.5`, else `add_route_capacity`.

Parameters live in an optional `llm_backlog:` mapping in `config/site_config.yml` with exactly the `BacklogParams` field names; absent keys take the defaults above.
There is no other configuration.

### Output

`--json PATH` writes:

```json
{"generated_at": "2026-09-30T00:20:00+00:00", "window_days": 7, "params": {"...": 0},
 "verbs": {"chapter-agenda": {"days": 7, "backlog": 1625, "classes": {"queued": 1625},
   "blocked_or_held": {"blocked": 0, "policy_held": 0, "errored": 0}, "slope": -780.07, "mean": 4277.1,
   "trend": "shrinking", "throughput_per_day": 713.9, "drain_days": 2.28, "constrained": false, "action": null,
   "unclassified": {}}},
 "unclassified_tokens": {"tags": {"some-new-token": 3}}}
```

`--markdown PATH` writes one table (verb, backlog, trend, drain, constrained, action, blocked/held, unclassified) for `GITHUB_STEP_SUMMARY`.

### Golden results (the acceptance test must reproduce these from the committed fixture, `window_days=7`, defaults)

| Verb | days | backlog | slope | mean | trend | throughput/day | drain days | constrained | action |
|---|---|---|---|---|---|---|---|---|---|
| chapter-agenda | 7 | 1,625 | −780.07 | 4,277.1 | shrinking | 713.9 | 2.28 | false | none |
| chapter-locator | 7 | 556 | 85.36 | 356.1 | growing | 157.1 | 3.54 | false | none |
| tagger | 7 | 25 | −0.86 | 23.7 | flat | (168.9, unreliable) | (0.15) | false | none |
| prelabeler | 7 | 68 | −18.82 | 121.6 | shrinking | (168.9) | (0.40) | false | none |
| prelabeler-shadow | 7 | 122 (all `ingress_limited`) | 18.21 | 86.1 | growing | (168.9) | (0.72) | **true** | `raise_ingress_quota` |
| moments | 7 | 17 (+1 `errored`, 1,210 `policy_held` reported, not counted) | −13.39 | 37.1 | shrinking | (23.1) | (0.73) | false | none |

The shadow prelabeler is correctly flagged: its lane is closed by its own daily write budget (`topic-tags:prelabeler-shadow` 740 units) by design; it is a fine demonstration that
`ingress_limited` backlog is separated from route capacity.

## File-by-file changes

| File | Change |
|---|---|
| `citypods/ops/backlog_trend.py` | **new** (above) |
| `tests/test_backlog_trend.py` | **new** (test plan) |
| `tests/fixtures/backlog_trend/run_events_2026_09_22_30.json` | **already committed** with this doc |
| `config/site_config.yml` | optional `llm_backlog:` block with the five defaults, commented (PR1) |
| `.github/workflows/backlog-trend.yml` | **new** (PR2) |
| `ARCHITECTURE.md` (Ops/QA row), `CHANGELOG.md`, `review/11` | doc-update contract |

**Do not modify:** `citypods/stages.py`, `citypods/run.py`, `citypods/cli.py`, anything under `workers/`, `citypods/statesync.py`, any durable-state schema. If an implementation seems to need one of these, stop and raise it.

## Reading events (`main`)

1. `site = load_site_config(--site-config)`; `storage = make_storage(site, site.get("base_url", ""), Path(--output-dir))` (same as `citypods/llm_tag_review.py::_paths`).
2. `since = now − (window_days + 2) days`. List `storage.list_objects(f"{STATE_PREFIX}/run_events/")` and keep keys whose basename timestamp prefix
   (`YYYYMMDDTHHMMSS…`, the first 15 characters of the file name) is ≥ `since.strftime("%Y%m%dT%H%M%S")`. Keys sort lexicographically by time.
3. `pull_state(storage, state_dir, only_paths=[rel, …])` with `rel = key[len(STATE_PREFIX)+1:]` (exact-key restore; no manifest needed), then `json.loads` each file;
   unreadable or non-object files are skipped and counted in the output (`"skipped_files": N`), never fatal.
4. `--state-dir DIR` skips storage and reads `DIR/run_events/*.json` directly (used by tests and local runs).

## CLI

`python -m citypods.ops.backlog_trend [--site-config config/site_config.yml] [--output-dir output] [--state-dir DIR] [--window-days N] [--json PATH] [--markdown PATH] [--fail-on-unclassified]`

Exit codes: 0 report produced; 1 only with `--fail-on-unclassified` and at least one unclassified token; 2 no events found.

## Workflow (PR2)

`.github/workflows/backlog-trend.yml`: `schedule: "20 0 * * *"` and `workflow_dispatch`; `permissions: contents: read`; `concurrency: { group: backlog-trend }`;
`timeout-minutes: 15`; steps mirror `tag.yml` for checkout and Python setup (`pip install -e ".[storage]" -c constraints/prod.txt`), export only
`B2_ENDPOINT, B2_KEY_ID, B2_APP_KEY, B2_BUCKET, B2_PUBLIC_BASE_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL` (no LLM or dispatch secrets),
run the module with `--json "$RUNNER_TEMP/backlog_trend.json" --markdown "$RUNNER_TEMP/backlog_trend.md" --window-days 7 --fail-on-unclassified`, append the markdown to
`$GITHUB_STEP_SUMMARY`, and upload the JSON as an artifact (`retention-days: 30`). It writes nothing to storage or the repository.

## Test plan (`tests/test_backlog_trend.py`)

1. `test_golden_results_from_committed_fixture`: loads the fixture, runs `analyze_all` with defaults, asserts every cell of the golden table (ints exact, floats to 2 places).
2. `test_token_ownership_ignores_sibling_verb_tokens`: a `tags` event with prelabeler tokens yields zero tagger backlog from those tokens and no unclassified entry.
3. `test_unknown_token_is_reported_not_counted`: an unknown token appears in `unclassified` and in `unclassified_tokens`, not in `backlog`.
4. `test_blocked_policy_held_and_errored_do_not_count`: `missing-agenda-artifact`, `rollout-dispatch-cap`, `llm-error` are in the reported subtotals but not in `backlog`.
5. `test_last_event_of_day_wins_and_ran_sums_all_events`.
6. `test_interrupted_events_are_ignored`.
7. `test_insufficient_data_below_min_points`.
8. `test_trend_deadband` (flat inside the threshold, growing/shrinking outside) and `test_ols_slope_exact` on a hand-computed series.
9. `test_constrained_requires_drain_for_reliable_verbs` (growing locator with short drain is not constrained; growing with drain above 14 days is) and
   `test_unreliable_verbs_use_trend_only`.
10. `test_action_split_ingress_vs_route_capacity` (≥ 50% `ingress_limited` gives `raise_ingress_quota`).
11. `test_params_from_config_defaults_and_overrides` and `test_unknown_config_key_rejected`.
12. `test_main_reads_state_dir_and_writes_json_and_markdown` (tmp dir with a few event files; asserts exit codes 0/1/2 including `--fail-on-unclassified`).
13. `test_main_storage_path_uses_exact_keys` with a fake storage whose `list_objects` returns keys and whose `get_file` is recorded, asserting only keys at or after `since` are requested.

## Acceptance criteria

- `python -m citypods.ops.backlog_trend --state-dir <dir with the fixture's events> --window-days 7` reproduces the golden table.
- `ruff check .`, `ruff format --check .`, and the full test suite pass; no file listed under "Do not modify" changed; the workflow runs green once on `workflow_dispatch` and its summary shows a
  table for all six verbs against live storage with `skipped_files` 0 and no unclassified tokens (if it fails on an unclassified token, classify it in `STAGE_TOKENS` and re-run).
- The JSON artifact for the live run shows `chapter-agenda` with backlog on the order of the latest `run_events` value and a `shrinking` or `flat` trend consistent with the Actions logs.

## Sequencing

PR1: module, tests, config block, docs (no workflow). PR2: workflow and the first live run. They are independent of every other phase; P1 (judge ledger) and P6 (leagues) consume `analyze_all`.

## Migration, backfill, rollback

None: read-only over existing events. Rollback is deleting the workflow; the module has no callers until P6.

## Later (explicitly not P0)

- A status-page row for the six verbs (`/admin/status`), reading the same JSON.
- A `llm_completed` quality counter in `TagsStage` so tagger throughput becomes reliable (then flip `throughput_reliable`).
- Extending `VERBS` for judge and adjudicator lanes (P1/P3).
