# Maintainer guide: handling an "Add a city" issue

When an [add-city issue](ISSUE_TEMPLATE/add-city.yml) comes in, the goal is to land one or
more `cities/*.yml` feeds — **one feed per meeting body** (City Council, Planning & Zoning,
Board of Adjustment, …) — and verify the right bodies are approved vs. denied.

## 1. Identify the provider and source
From the city/platform in the issue, find the source URL:

| Provider | How to find `source` |
|---|---|
| **granicus** | `https://<sub>.granicus.com/ViewPublisherRSS.php?view_id=N&mode=vpodcast` — try view_ids until you find the populated meeting feed. |
| **civicplus** | CivicMedia channel RSS: `<site>/RSSFeed.aspx?ModID=92&CID=<channel>` (see `<site>/rss.aspx`). |
| **civicclerk** | OData API host: `https://<tenant>.api.civicclerk.com` (+ optional `category_id`). |
| **swagit** | The archive view page: `https://<tenant>.new.swagit.com/views/default/<slug>`. |

`DownloadFile.php` (Granicus) 302-redirects to a real MP4 even if the RSS says WMV — that's fine.

## 2. Add a base ("all meetings") city YAML
Copy [`cities/_template.yml`](../cities/_template.yml) to `cities/<slug>.yml`, set
`provider` + `source` + metadata. This is the combined feed (no `body:` filter). Title it
`"<City> — All Meetings"`.

## 3. Review the bodies — approved (✓) vs denied (✗)
```bash
citypods bodies <slug>
```
This lists every meeting body with its count + latest date, marking each ✓ (will become a
feed) or ✗ (matched the `body_exclude` denylist — procurement, public-info programs, etc.).

**Verify the denylist is right for this city:**
- A real deliberative body marked ✗? Remove the offending term from this city's `body_exclude`
  (it defaults from `site_config.yml`; override per city in the YAML).
- A non-body marked ✓ (e.g. a recurring program)? Add a term to the city's `body_exclude`.

## 4. Generate per-board feeds
```bash
PYTHONPATH=. python scripts/generate_board_cities.py <slug> \
    --base-slug <slug> --title-prefix "<City>" --write
```
This writes one `cities/<slug>-<body>.yml` per body with ≥ `min_meetings_per_body` meetings in
the last 12 months (configurable), skipping denylisted bodies and merging `" - subtype"` /
`": panel"` variants. Review the generated files: fix titles, merge obvious variants (broaden a
`body:` substring and delete siblings), delete any you don't want.

## 5. Validate and open a PR
```bash
ruff check . && ruff format --check . && pytest
```
Open a PR; CI + the per-PR preview build validate the feeds. Note: **Swagit/CivicPlus** feeds
re-host audio (ffmpeg → B2), bounded by `materialize_budget_per_run`, so a new city backfills
over several scheduled deploys — that's expected.
