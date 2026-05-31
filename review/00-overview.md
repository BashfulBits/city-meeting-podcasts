# City Meeting Podcasts — Codebase Review (overnight, 2026-05-31)

This is a thorough review produced autonomously. It is organized so the **analysis/proposals
land first** (most valuable if the run is cut short), then plan/memory updates, then code
changes on per-topic branches.

## Documents

| # | File | What it covers |
|---|------|----------------|
| 00 | `00-overview.md` | This index + executive summary |
| 01 | `01-feature-brainstorm.md` | 50 candidate features, grouped, with value/effort/cost |
| 02 | `02-architecture.md` | Architecture changes to support those features + steps |
| 03 | `03-resource-model.md` | **Parametric** cost/time monitor + projection proposal (the big one) |
| 04 | `04-audit-bugs-security.md` | Bug + security audit, severity-ranked |
| 05 | `05-endpoint-contract-tests.md` | Inventory of every external endpoint + a test/monitoring plan |
| 06 | `06-misc-improvements.md` | Smaller codebase improvements |
| 07 | `07-code-change-plan.md` | The per-topic branches I'll create in phase 3, in order |

## Executive summary

**State of the codebase (5,000 LOC core, 6,600 incl. scripts; 157 tests; 4 providers; 80 feeds).**
The architecture is in very good shape after the episode-record refactor (R1–R3) and the durable-state
work. The enrichment-stage pipeline is the right abstraction and is already paying off — links,
chapters, and transcripts slotted in without structural change. The split-hash invalidation
(`audio_spec_hash` vs `feed_content_hash`), content-addressed audio, stable provider-independent
`uid`, and bucket-as-source-of-truth state are all sound and would survive a 10–100× scale-up.

**Highest-value findings:**

1. **One real bug**: `scripts/generate_board_cities.py` imports `citypods.media._source_key`, which was
   removed in the R1 refactor — the script `ImportError`s on run. This is the tool that onboards
   per-board feeds, so it's effectively broken for scaling. (Audit #B1; fix is one line.)
2. **No automated coverage of the external endpoints** we depend on. Every provider integration is
   a screen-scrape or undocumented API; when a city's platform changes HTML/JSON we currently find
   out via the feed-health audit (symptom) rather than a contract test (cause). Doc 05 proposes a
   marked live-contract suite + a `check_endpoints.py` monitor that names the exact broken pattern.
3. **Resource projection is the strategic gap.** There is no way to answer "what does 1,000 cities
   cost?", "what if I host all audio?", or "how many days to drain the backlog?" Doc 03 specifies a
   parametric model (a `projection` module + committed report + GitHub job-summary + a static
   what-if admin page) that turns those into a slider.
4. **Security: the trust boundary changes at Phase 5.** Today all source URLs are maintainer-authored,
   so SSRF/abuse is a non-issue. The moment cities are onboarded from GitHub-issue submissions, the
   build fetches attacker-influenced URLs — this needs an allowlist/validation gate before Phase 5.
   (Audit #S1.)

**Nothing here is on fire.** The bug is in a dev script, not the deploy path; the security items are
forward-looking; the rest is opportunity. The codebase is unusually clean for its age.

## How to consume the code changes (phase 3)

Each topic is a local branch, committed but **not pushed** (per your instruction). `07-code-change-plan.md`
lists them with the exact `gh pr create` you can run in the morning. Branch naming: `review/<topic>`.
