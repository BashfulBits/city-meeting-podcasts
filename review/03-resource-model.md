# Resource Usage Monitor & Projection — full proposal

**Goal:** a *parametric* model that answers, at any moment, the questions you actually ask when
scoping growth:

- *What does the storage bill look like at N cities?*
- *What if I host all audio (even where a direct video link exists)?*
- *How many days of 6-hour Actions runs to drain the current backlog?*
- *If I add a new feature (e.g. transcripts) that touches every episode, how long to catch up?*

It renders in **three places**: (1) a committed JSON+Markdown report, (2) a GitHub Actions
job-summary table each run, and (3) a static **what-if admin page** on the domain with sliders.
The first two are steady-state/actuals; the admin page is the dynamic modeler.

---

## 1. The model (closed-form; all knobs explicit)

### Inputs (knobs)

| Symbol | Knob | Default | Source |
|---|---|---|---|
| `F` | number of feeds | measured (80) | `len(cities)` |
| `E` | episodes retained+materialized per feed | 50 | `max_episodes` |
| `D` | avg meeting duration (hours) | 2.0 | measured from `Episode.duration` |
| `kbps` | audio bitrate | 96 | `audio_max_kbps` |
| `host_frac` | fraction of feeds whose audio we host | measured (~0.6) | HLS feeds always; direct only if `extract_audio` |
| `sec_per_ep` | wall-seconds to materialize one episode | measured (~90) | `run_history` (Change 2) |
| `cycle_h` | hours between Actions runs | 6 | `schedule_cron` |
| `time_budget_h` | usable wall-time per run | 5.0 | < 6h hard limit, with headroom |
| `safety` | fraction of the window we'll fill | 0.8 | config |
| `cap` | hard per-run episode cap (optional) | 25 (today) | `materialize_budget_per_run` |
| `mtg_per_wk` | new meetings per feed per week | 1.0 | measured from publish cadence |

### Storage

```
bytes_per_episode = kbps × 1000 / 8 × 3600 × D          # = 0.00045·kbps·D GB
storage_GB        = F × E × D-factor × host_frac        # only hosted feeds count
monthly_cost_$    = max(0, storage_GB − 10) × $0.006    # B2 $6/TB/mo, first 10 GB free
```

B2 specifics baked in: **egress is $0** (free via Cloudflare Bandwidth Alliance / R2 native),
Class-A uploads free, Class-B/C transactions effectively free at this volume. So **cost is
storage-only** — a clean single term. (The model still surfaces transaction counts for sanity.)

### Throughput & backlog drain

```
T_time  = floor(time_budget_h × 3600 × safety / sec_per_ep)   # episodes per run by wall-time
T       = min(T_time, cap)  if cap else T_time                 # per-run throughput
R       = 24 / cycle_h                                          # runs per day
capacity_per_day = T × R
drain_days = backlog / capacity_per_day
```

Steady-state check: `inflow_per_day = F × mtg_per_wk / 7` must be `< capacity_per_day`, else the
backlog grows without bound (the model flags this).

### Per-feature backlog

A new per-episode feature (transcripts, host-all, summaries) creates a one-time backlog
`B = F × E × affected_frac` and adds `bytes_per_ep_feature` to storage. The model takes a
**feature spec** `{sec_per_ep, bytes_per_ep, affected_frac, budget_key}` and reports its drain
time and steady-state storage delta independently (each stage has its own budget).

---

## 2. Worked scenarios (computed; see `scripts`/projection for live values)

Audio size (mono AAC): **96 kbps × 2 h = 86 MB/episode**; 64 kbps = 58 MB; 128 kbps = 115 MB.

### Storage & monthly B2 cost (E=50, D=2h, 96 kbps, host_frac=1.0)

| Feeds | Audio stored | Monthly B2 |
|------:|-------------:|-----------:|
| 80 | 0.35 TB | **$2.01** |
| 200 | 0.86 TB | **$5.12** |
| 500 | 2.16 TB | **$12.90** |
| 1,000 | 4.32 TB | **$25.86** |
| 5,000 | 21.6 TB | **$129.54** |

**Headline: storage is not the constraint.** 1,000 cities of full audio is ~$26/month. Even
5,000 is ~$130/month. The free 10 GB covers a dev/local footprint entirely.

### "Host all audio" delta (today, 80 feeds)

- Current (~48/80 feeds hosted: 44 Swagit HLS + 4 `extract_audio`): **~207 GB → $1.18/mo**
- Host-all 80 feeds (add the 32 direct Granicus): **~346 GB → $2.01/mo** (+$0.83/mo)

So host-all is essentially free on storage; the cost is the **one-time encode backlog** (below).

### Backlog drain time — *this is where the real constraint lives*

| Scenario | Backlog | @ current cap=25/run, 6h | @ time-bounded (5h, 90s/ep) |
|---|---:|---:|---:|
| Host-all, 200 feeds | 10,000 eps | **100 days** | **~14 days** |
| Host-all, 1,000 feeds | 50,000 eps | **500 days** | **~70 days** |

At 60s/ep the time-bounded numbers roughly halve. **The `materialize_budget_per_run = 25` cap is
the bottleneck, not storage, not the 6-hour limit** — a 6h run at 90s/ep could do ~190 episodes,
8× the current cap. (Change 3 in doc 02 makes this budget auto-derived from the wall-clock target;
Change 1+2 measure the real `sec_per_ep` so the projection is calibrated, not guessed.)

### Steady-state keeps up easily

1,000 feeds × 1 meeting/week ≈ **143 new episodes/day** vs a time-bounded capacity of **~640/day**
(6h cycle). Comfortable headroom — so once the initial backlog drains, normal operation never
saturates the window. This is exactly the regime where "catch-up mode" (#41) should raise budgets
to chew through any feature backlog, then idle.

---

## 3. Implementation

### 3a. `citypods/projection.py` (pure, testable)

A dependency-free module with:
- `@dataclass ModelInputs` (all knobs above, with defaults).
- `@dataclass FeatureSpec` (sec_per_ep, bytes_per_ep, affected_frac, name).
- `project(inputs, features=[]) -> Projection` returning storage_GB, monthly_cost, per-run
  throughput, capacity/day, inflow/day, per-feature drain days, and an `at_scale(F)` helper.
- `measured_inputs(cities, run_history, records) -> ModelInputs` that derives `F`, `E`, `D`,
  `host_frac`, `sec_per_ep`, `mtg_per_wk` from **real data** (run history + record store +
  episode durations) so defaults are replaced by observed values where available.

Pure functions → unit-tested with fixed inputs (golden numbers from §2).

### 3b. Committed report — `citypods report` CLI + `docs/admin/report.json`

`citypods report` (new CLI subcommand) loads cities + state, calls `measured_inputs` + `project`,
and writes:
- `docs/admin/report.json` — machine-readable (current actuals + a few standard scenarios).
- prints a Markdown table to stdout (for the job summary).

Runs at the end of `deploy.yml`; the JSON is published (see admin page) and the Markdown is
appended to `$GITHUB_STEP_SUMMARY` so every run shows backlog/cost/throughput at a glance.

### 3c. GitHub Actions job summary (steady-state, per run)

In `deploy.yml`, after build:
```yaml
- name: Resource summary
  run: citypods report --markdown >> "$GITHUB_STEP_SUMMARY"
```
Gives, on every run's page: current stored GB + $/mo, backlog per stage, episodes done this run,
projected drain days at the current budget, and a ⚠ if inflow > capacity. Private to the repo,
zero infra.

### 3d. Static what-if admin page — `docs/admin/index.html`

The dynamic modeler. Build-time generated, but **the model runs client-side in JS** so the
sliders are live with no server:

- At build, embed `report.json` (current measured inputs + run-history-derived rates) as
  `<script id="model-data">`.
- A small vanilla-JS reimplementation of `project()` (the formulas are trivial closed-form) recomputes
  on every slider change. Sliders/inputs: **feeds (80→5,000), E, duration, bitrate, host-all toggle,
  sec/ep, cycle hours, per-run cap (or "time-bounded" toggle), new feature {sec/ep, bytes/ep,
  % affected}.**
- Outputs update live: storage TB, $/mo (with a B2 free-tier note), per-run throughput, capacity/day,
  **days-to-drain backlog**, steady-state keep-up ✅/⚠, and a small chart (cost vs feeds; drain-days
  vs per-run budget). Chart via inline SVG or a tiny lib — no heavy deps.
- Pre-set scenario buttons: "Current", "2–3× growth", "Texas statewide", "1,000 cities",
  "Nationwide top 500", "Host all audio", "Add transcripts".

**Privacy note (you flagged this):** GitHub Pages is public, so `docs/admin/` is world-readable.
Recommendation: the page is mostly a *calculator* (formulas + your chosen knobs), which is not
sensitive. The only sensitive-ish datum is *current actual stored GB / backlog*. Options, pick one:
1. **Publish the calculator, omit live actuals** (sliders default to measured values but the page
   doesn't announce "we currently store X" — low sensitivity, simplest). *Recommended.*
2. Put `report.json` (the actuals) behind a **Cloudflare Access** rule on `/admin/*` (free for a
   few users) while the calculator stays public.
3. Keep actuals only in the repo (job summary + committed `review/`-style report) and make the
   public page a pure calculator with neutral defaults.

I'll implement (1) by default — calculator public, actuals available but understated — and document
(2) as the lock-down path in the admin page header.

### 3e. JS/Python parity test

A test that runs a handful of inputs through the Python `project()` and asserts the JS produces the
same numbers (extract the JS formulas, run via `node` if available, else assert the embedded
constants match). Prevents the calculator drifting from the source of truth.

---

## 4. Dependencies on doc 02

- **Change 1 (stage cost accounting)** and **Change 2 (run_history.jsonl)** are prerequisites for
  *measured* `sec_per_ep` and per-stage backlog. Without them the model still works on **defaults**
  (everything in §2 is computed from defaults), but calibration needs them. I'll implement the model
  to gracefully use defaults when history is absent, and tighten as history accrues.
- **Change 3 (dynamic budgets)** is the *action* the model motivates: the report should recommend a
  per-run budget = `floor(time_budget × safety / sec_per_ep)` and flag when the static cap is the
  bottleneck (as it is today at 25).

## 5. What the model will tell you on day one (with current defaults)

- Current storage ≈ **0.2 TB, ~$1.20/mo**. Trivial.
- Backlog to host-all today (32 Granicus feeds × ~50 eps ≈ 1,600 eps) ≈ **2–3 days** time-bounded,
  but **~64 days at the cap of 25**. → *raise the cap or switch to time-bounded.*
- At 1,000 cities: **~$26/mo storage**, steady-state easily handled, initial full backfill **~70 days**
  time-bounded (vs 500 at cap=25). → *the budget, not the bill, governs how fast you can grow.*

## 6. "Maximize the 6-hour window" recommendation (concrete)

1. Implement Change 1+2 to measure `sec_per_ep` (I'll add the plumbing; one or two real runs
   calibrate it).
2. Switch `materialize_budget_per_run` from a flat 25 to **time-bounded** (Change 3):
   `budget = floor(5h × 0.8 / sec_per_ep)`. At 90s/ep that's ~160/run = ~640/day — 25× today.
3. Add **catch-up mode**: when `built` (re-renders from feature churn) is low, give the freed time
   budget to the materialize/transcript stages. The model's per-stage backlog tells the run how to
   split the window.
4. Keep a *storage* guardrail only if you later want one (you chose no hard cap); the model still
   prints projected $/mo so a surprise is impossible.

---

## 7. Per-feature recurring cost & ASR throughput (captured 2026-05-31)

Cost assumptions: avg meeting **2 h = 120 min**, **~50 meetings/feed/yr** materialized, **avg city
≈ 12 feeds** (range: small town 1–3, Fort Worth ~17, Dallas ~35). ASR anchors: Deepgram
**$0.0043/min**, OpenAI Whisper **$0.006/min**, **self-hosted faster-whisper on Actions = $0 cash**.
LLM anchors: GPT-4o-mini (~sub-cent/meeting), Claude Haiku (~1–3¢/meeting). Transcript/summary text
storage is negligible (~100–150 KB/meeting).

### Recurring cost is dominated by ASR (#1); everything downstream is pennies
| Feature | Per meeting | Per city/yr (~12 feeds) | Notes |
|---|---|---|---|
| #1 transcripts (API) | $0.52–0.72 | **$310–430** *(or $0 self-host)* | the only material cost |
| #2 summaries | $0.004–0.03 | $2–18 | needs a transcript |
| #3 per-item summaries | $0.01–0.05 | $6–30 | |
| #4 tags | $0.002–0.01 | $1–6 | or $0 rule-based on agenda titles |
| #6 search | $0 | $0 | static index; value gated on #1 |
| #8 votes | ~$0 (CivicClerk/Granicus metadata) | ~$0 | parser, no ASR/LLM |
| #9 translation (summaries only) | $0.003–0.05 | $2–15 | full-transcript MT ≈ $2.75/mtg → avoid |
| #15 soundbites | ~$0.01 | ~$6 | needs #1 |

### #1 reuse-first → only ~34 feeds actually need ASR
Provider-supplied transcripts cover most of the catalog:
| Provider | Feeds | Transcript source |
|---|---:|---|
| Swagit | 44 | provider `/videos/{id}/transcript` — **free** |
| CivicClerk | 1 | `transcriptionUrl` / `.srt` — **free** |
| Granicus | 33 | **needs ASR** |
| CivicPlus | 1 | **needs ASR** |

→ ~57% of feeds get free transcripts; the ASR backlog/cost is roughly **halved**.

### Self-hosted ASR throughput (free GitHub Actions, once backlog is drained)
faster-whisper "base" int8 on `ubuntu-latest` (4 vCPU) ≈ **4–6× realtime** → a 2 h meeting ≈
**20–30 min** compute. Usable window ≈ 4 effective compute-hours/run.

| Setup | Per run | Per week |
|---|---:|---:|
| 1 job, 6 h cron (4 runs/day) | ~9–10 | **~250–270** |
| 1 job, 4 h cron (6 runs/day) | ~9–10 | **~380–400** |
| matrix of 4 jobs (4 h cron) | — | ~1,500 |
| matrix of 8 jobs | — | ~3,000 |

Context: current ~80 feeds generate **~55 new meetings/week** → a single free job covers today's
inflow ~6×. 1,000 feeds (~700 new/week) needs ~2–3 parallel jobs. Caveats: GitHub fair-use (keep to
≤4–8 jobs, not 24/7 on 16 runners); "base" quality is fine for search/summaries, "small"/"medium"
halve/quarter throughput; run ASR in its **own scheduled workflow** so it doesn't contend with deploy.
**The free tier is not the constraint on new inflow — only the initial backlog takes calendar time
(the projection's drain-days number).**

### Conclusion
The strategic cost decision is **how to do #1**: ~$310–430/city/yr via API **or ~$0 self-hosted**
(trading throughput). Reuse-first halves it. Once #1 exists, the rest of Group A is pennies-per-meeting.
The two high-value $0 items needing **no** transcripts: **#51 (meetings link)** and **#8 (CivicClerk
votes)**.
