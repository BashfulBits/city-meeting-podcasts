# Maintainer guide: handling an "Add a city" issue

When an [add-city issue](ISSUE_TEMPLATE/add-city.yml) comes in, the goal is to land one or
more `config/feeds/*.yml` feeds — **one feed per stable body or approved city family** (City Council, Planning &
Zoning, TIF boards, …) — and verify complete historical coverage and explicit exclusions.

Config lives under `config/`: per-board feeds in `config/feeds/`, shared per-city fields
(`city_website`, `meetings_url`, `state`, `colors`) in a `config/cities/<entity-slug>.yml`
entity file that each feed references via `city: <entity-slug>`.

## 1. Identify the provider and source
From the city/platform in the issue, find the source URL:

| Provider | How to find `source` |
|---|---|
| **granicus** | `https://<sub>.granicus.com/ViewPublisherRSS.php?view_id=N&mode=vpodcast` — try view_ids until you find the populated meeting feed. |
| **civicplus** | CivicMedia channel RSS: `<site>/RSSFeed.aspx?ModID=92&CID=<channel>` (see `<site>/rss.aspx`). |
| **civicclerk** | OData API host: `https://<tenant>.api.civicclerk.com` (+ optional `category_id`). |
| **swagit** | The archive view page: `https://<tenant>.new.swagit.com/views/default/<slug>`. |

`DownloadFile.php` (Granicus) 302-redirects to a real MP4 even if the RSS says WMV — that's fine.

## 2. Add the entity record + a base ("all meetings") feed YAML
First, if the city isn't onboarded yet, copy
[`config/cities/_template.yml`](../config/cities/_template.yml) to
`config/cities/<entity-slug>.yml` and fill in `city_website`, `meetings_url`, `state`, `colors`.

Then copy [`config/feeds/_template.yml`](../config/feeds/_template.yml) to
`config/feeds/<slug>.yml`, set `city: <entity-slug>` + `provider` + `source` + metadata. This is
the combined feed (no `body:` filter). Title it `"<City> — All Meetings"`.

## 3. Assess the complete historical archive before publication

Use a frontier agent with maintainer review for the initial taxonomy assessment. Fetch all available
provider pages/views and merge historical records append-only before making feed decisions. Record
source caps, unavailable periods and duplicate views; a recent RSS window is not a full archive.

Classify every historical label into a verified existing body, an approved aggregate family, a new
legitimate body, an explicit non-meeting exclusion, or a documented unresolved question. Counts and
recency do not decide eligibility: sparse and retired legitimate bodies still need coverage. Bond
and charter programs use one city feed per family, preserving program/year in official titles.
The default is one TIF aggregate and a separate PID aggregate per city, with city public-input
and public-briefings families for verified proceedings. Program/year stays in official titles.
These subscription defaults never establish identity from a topic word alone. Promotions,
ceremonies, staff training and municipal TV shows get evidence-backed exclusions.

Approve policy families once, then encode selectors and remedy policy together. Do not create one
feed per raw spelling. Preserve official titles, recording identities and archive records. Replay
selectors against all available historical recordings and negative cases, checking newly included
recordings as well as unmatched labels. Resolve ambiguous identity with official evidence.

Freeze traceable examples and separate gold under [`evals/remedy/`](../evals/remedy/README.md),
following [review/51](../review/51-unexpected-body-remedy-flow.md). Once its planned runner ships,
run the approved evaluation/admission flow and retain dated results for the actual routes/efforts
used. Until then, record the frontier assessment and maintainer-reviewed selector replay there;
do not claim a completed model evaluation. Initial city coverage needs a documented disposition
for every available historical label and all legitimate recordings assigned to an appropriate feed.
Unresolved legitimate recordings need explicit maintainer disposition before publication.
Incomplete archives may onboard through a documented maintainer exception listing available
dates/views, missing periods/caps, retrieval attempts, approval link and the visible coverage
limitation. Assign every available legitimate recording before publication; never label partial
coverage complete. Newly available history reopens the affected assessment. Store the exception
in `config/remedy.yml` once P1 ships; until then include it in the city onboarding PR and committed
coverage evidence.

The existing inventory command is an aid to this assessment:
```bash
citypods bodies <slug>
```
This lists every meeting body with its count + latest date, marking each ✓ (will become a
feed) or ✗ (matched the `body_exclude` denylist — procurement, public-info programs, etc.).

**Verify the denylist is right for this city:**
- A real deliberative body marked ✗? Remove the offending term from this city's `body_exclude`
  (it defaults from `site_config.yml`; override per city in the YAML).
- A non-body marked ✓ (e.g. a recurring program)? Add a term to the city's `body_exclude`.

## 4. Generate exploratory drafts, then apply the approved taxonomy
```bash
PYTHONPATH=. python scripts/generate_board_cities.py <slug> \
    --base-slug <slug> --title-prefix "<City>" --write
```
This writes one `config/feeds/<slug>-<body>.yml` per body with ≥ `min_meetings_per_body` meetings in
the last 12 months (configurable), skipping denylisted bodies and merging `" - subtype"` /
`": panel"` variants. The generator is an exploratory draft tool, not a historical coverage or approval gate. Its
recent-count threshold does not authorize excluding sparse/historical public bodies. Review output
against the complete archive policy: merge approved families, keep distinct identities separate,
encode evidence-backed exclusions, and include legitimate bodies missed by the generator. Add
positive/negative regression cases and the equivalent remedy guard for every new family policy.

## 5. Validate and open a PR
```bash
ruff check . && ruff format --check . && pytest
```
Open a PR; CI runs lint/tests, and the per-PR preview build renders feeds/pages from restored
state without live provider refresh. Live provider/feed checks are handled by the contract/live
validation workflows. Note: **Swagit/CivicPlus** feeds
re-host audio (ffmpeg → B2), bounded by the per-run **wall-clock enrich window**
(`run_time_budget_minutes` × `budget_safety`; a run also yields early if a newer build is queued),
so a new city backfills over several scheduled deploys — that's expected.
