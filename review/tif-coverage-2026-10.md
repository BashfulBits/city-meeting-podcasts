# Dallas and Fort Worth TIF aggregate subscription policy

**Status: Implemented in PR #1973 (merged 2026-10-02) · FROZEN**
Aggregate TIF feeds  and  are deployed in  and verified
by . Predecessor district configs were removed and registered as feed aliases.

## Decision and evidence

Publish one `dallas-tx-tif` and one `fort-worth-tx-tif` feed. These are subscription groups, not an
assertion that the districts are the same public body. Include district boards, joint TIF meetings,
and TIF oversight bodies. Preserve official episode titles, dates, links and identities.

The read-only provider archive census on 2026-10-02 found:

| City | All provider rows | All distinct raw labels | TIF labels | Included provider rows |
|---|---:|---:|---:|---:|
| Dallas | 5,344 | 1,090 | 143 | 284 |
| Fort Worth | 5,544 | 860 | 13 | 32 |

Dallas's 283 marker-labeled recordings span 2015-08-13 through 2026-09-30. One additional
2023-01-12 joint City Center / Downtown Connection / DDA recording (Swagit 206541) lacks a TIF
marker and is included by GUID, not by a broad DDA selector. Fort Worth's 32 rows span
2012-10-30 through 2025-08-06 and represent 16 clip IDs repeated across archive views. Counts are
provider observations, not claims of the final rendered episode count. Existing UID/view handling
is unchanged. Census coverage is the provider-visible archive, not a claim that every recording
ever made is still available.

The cities' own descriptions establish the family:
[city of Dallas reinvestment zone boards](https://dallascityhall.com/government/Boards-and-Commissions/Reinvestment-Zone-Board/Pages/default.aspx)
and [Fort Worth TIF districts](https://www.fortworthtexas.gov/departments/econdev/tif).
District numbers and names remain important evidence, but are unnecessary for membership in this
aggregate. Dallas's `TIF Ethics Advisory Commission` belongs as TIF oversight. Public improvement
districts, general economic-development committees and the Dallas Development Fund are separate.

## Selectors and residual risks

Use the existing normalized glob/substring selector union. Boundary globs recognize TIF and TIRZ
at the beginning, middle or end of a label; phrases recognize `tax increment` and `reinvestment
zone`. A naked `TIF` substring is unsafe: it matches **multifamily** and **Beautiful**. The frozen
census checks every observed raw label, including these three negative examples.

Rules handle varying numbers, district names, meeting qualifiers and duplicated Granicus titles.
They cannot interpret an abbreviation without any marker or establish a recording's identity from
its title alone. A future general-purpose meeting title that mentions TIF could match; new labels
must remain subject to evidence-based monitoring and the review/51 false-inclusion evaluation.
Do not expand the policy to every recording of a named neighborhood or any development project.
The unmarked Dallas joint meeting is an explicit exception requiring a fresh decision for a new GUID.

## Migration and processing impact

Remove seven Dallas district configurations and two Fort Worth district configurations. Their
slugs become `aliases` on the replacement aggregate, using the existing HTML redirects and
`itunes:new-feed-url` feed stubs. No old slug is simply dropped. Remove Dallas Economic
Development's TIF GUID 202470 inclusion; the aggregate covers that recording by its zone label.
Keep its non-TIF inclusions. The two aggregate feed sources and podcast authors retain their
predecessors' source namespace and UID inputs. Archived records and artifact pointers are not
removed, and no audio or enrichment pipeline version changes.

Selectors broaden eligible historical coverage. Ordinary catalog processing may subsequently
materialize previously excluded recordings under existing budgets; this is **not** a promise of a
free instantaneous backfill. No production build, upload, reset or re-encoding was executed locally.
RSS remains its configured recent window; historical eligibility does not enlarge retention limits.

## Verification and traceability

`tests/fixtures/tif_coverage/archives.json` freezes counts for every provider label and full GUID,
date and title evidence for included labels (one representative recording for each negative label).
Expectations encode this reviewed policy; they are not live provider tests. `tests/test_tif_coverage.py`
replays coverage, false inclusions, future variants, source namespace preservation and alias migration.
The fixture was bootstrapped from marker candidates; every positive label and the unmarked joint
exception were inspected. It is a selector regression set, **not** an independent model holdout.
Model evaluations require separate input-only manifests and adjudicated truth under `evals/`.

The dependent remedy-policy implementation exposes this policy to remedy and prevent district feed recreation even when an
LLM proposes it. Changes to the remaining historical backlog require category approval first.

The guard also receives reviewed `member_names` for district-name clues without TIF markers.
These clues only hold proposals for review; they never add a recording or broaden a selector.
Unknown policies, malformed member lists and misspelled keys fail configuration loading.

