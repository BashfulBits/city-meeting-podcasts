# Historical meeting coverage: category approval proposal

**Status: directions A–F approved, 2026-10-02; October 2026 batches (TIF in #1973, Arlington in #1980, Addison UDC in #1992, Pflugerville BOA in #1993) merged and deployed.** Residual policy applications, ambiguous identities, and remaining historical gaps are tracked under review/51 and review/evidence/unresolved-recording-cases.json.

## Scope and evidence

The original #1747 run artifact contains 817 unexpected normalized labels / 2,282 provider rows
from six sources. Replaying those findings against current selectors plus the proposed TIF change
leaves **680 labels / 2,011 rows**. This is a historical inventory, not a live count of new meetings.
The read-only full Dallas/Fort Worth archives were separately refreshed on 2026-10-02 for the TIF
census. #1623 still reported the same six affected cities on 2026-10-01. Implementation must refresh
each source and restore append-only records before applying its recommendations; the September
snapshot alone must never authorize a new write.

| City | Residual labels | Provider rows |
|---|---:|---:|
| Addison | 4 | 13 |
| Arlington | 3 | 3 |
| Dallas | 150 | 572 |
| Denton | 183 | 200 |
| Fort Worth | 339 | 1,222 |
| Pflugerville | 1 | 1 |

Fort Worth has repeated clip observations across views. Counts are provider rows, not unique
meetings or final RSS episodes. Denton's historical Granicus source must be reconciled with its
current Swagit source and reviewed UID joins; never create duplicate feeds per provider or silently
move records between namespaces. Some source archives are capped: missing later recordings are
not proof a body retired.

The complete inventory is [the proposal CSV](evidence/historical-feed-proposals-2026-10.csv).
Every residual label has a stable source+label ID, category, proposed action, date span, row count,
example title/GUID, and confidence. Candidate slugs are grouping aids, **not executable approvals**.
Grouping began with explicit label rules and was reviewed for body, dated-series and public-input
patterns. These are agent recommendations from bounded title evidence, not independently
adjudicated ground truth. Low-confidence rows name the missing identity/recording evidence.

## Approve seven directions, then review small implementation PRs

Approval of a direction approves its subscription policy and evaluation rubric. It does not
approve every guessed alias in the CSV. Implementation PRs must resolve identities, collect source
links, replay selectors and use the review/51 evaluation lane before applying a category. Keep each
PR self-contained: at most five decisions, normally one parent body or one city/family policy.
Rejecting it moves that decision into an issue for refinement, not an unchanged next-week PR.

| ID | Direction | Labels / rows | Recommended approval |
|---|---|---:|---|
| A | Existing-body aliases and subordinate/joint recordings | 20 / 61 | Expand verified parents; hold uncertain parent relationships |
| B | Missing named public bodies and legislative committees | 207 / 982 | One stable subscription per verified body; dated labels are aliases |
| C | Bond programs, charter review and redistricting | 63 / 156 | One city subscription per family across programs/years |
| D | Community meetings and public input | 160 / 296 | One city aggregate across districts and infrastructure projects |
| E | Official public briefings | 36 / 150 | Separate public-briefings feed where actual public proceedings exist |
| F | Civic media, ceremonies, promotion and staff training | 157 / 314 | Confirmed exclusions; preserve recordings/records; do not create meeting feeds |
| G | Unresolved identity, notices, mixed recordings and unclear formats | 37 / 52 | Investigate first; verified genuine one-offs may use Other Public Meetings |

The maintainer has approved A's verified parent repairs, B's verified missing named bodies,
C's cross-program family policy, D's city public-input aggregates,
E's city public-briefings aggregates, and F's exclusion principle.
Separate city PID aggregates, including the future-city default, are also approved.
Exact selectors and ambiguous individual recordings still need evidence. Approval of A/B does
not approve guessed ownership. G remains investigation-first; any catch-all subscription needs
separate approval. Verified changes proceed in self-contained PRs of at most five decisions.

## A — Repair ownership before adding feeds

- Dallas Area Partnership's long corporation names and `LGC` abbreviation belong to its existing
  parent if confirmed. Urban Design Peer Review / Panel should reuse the existing peer-review feed.
- Fort Worth Research & Innovation LGC has a configured duplicated `Local Government Corporation`
  selector; repair the selector instead of generating another feed. Art Commission variants should
  reuse Public Art Commission after source confirmation; Water Task Force needs the same check.
- Dallas `DDF Board Meeting` and `CPC Bus Tour` need explicit ownership evidence. DDF is a distinct
  corporation, not automatically an Economic Development committee; CPC's activity is not
  automatically an ordinary Planning Commission session. The CSV targets are candidates to verify.
- Arlington `ZBA Regular Session` points toward a Zoning Board of Adjustment parent, but no such
  feed exists in this checkout; create that verified parent once, not a recurring UUID exception.

Implement normalization for dates, punctuation, duplicated provider titles and documented aliases;
never merge solely because of shared city or topic tokens. Subordinate/joint meetings can use exact
GUID inclusions when no safe recurring rule exists. The remedy validator must understand the parent
relationship and refuse unrelated body owners.

## B — Create named parents, not feeds for each spelling

Proposed body families to investigate and consolidate before cutting implementation PRs:

| City | Proposed parents | Specific cautions |
|---|---|---|
| Addison | Community Partnership; UDC Advisory | Bond Advisory belongs in C, not a separate bond-year feed |
| Arlington | Housing Finance Corporation; Zoning Board of Adjustment | One surviving recording still establishes a possible independent body |
| Dallas | Building Inspection Advisory/Examining/Appeals; Subdivision Review; Arts District Sign Advisory; Youth Commission; Workforce/Education/Equity; historical named council committees and task forces | Arts District Sign is distinct from Special Sign District; verify committee succession rather than assuming similar topics mean identity |
| Denton | Airport Advisory; Animal Shelter Advisory; Environment; Community Partnership; Hotel Occupancy Tax; Development Code Review; Library; Capital Improvement Advisory; Public Art | Collapse `on <date>` labels before counting recurrence; verify renamed Hotel Occupancy variants and cross-provider continuity |
| Fort Worth | Emergency Medical Response; Arts Funding; Aviation Advisory; Capital Improvements/Impact Fees; Minority Leaders/Citizens Council; Entrepreneurship/Innovation; CAP Council; Sports Authority; Disability committee; Human Relations; Business Equity; Pedestrian/Bicycle; Library; Park/Recreation; Alliance Airport; Municipal Court; Civil Service; Animal Shelter; Water/Wastewater advisory; Deferred Compensation; Housing/Economic Development; Race/Culture; transit board | Transportation-impact-fee CIAC and water/wastewater CIP bodies may be distinct; do not merge them on the acronym. Sports Authority/LGCs are independent corporations |
| Pflugerville | Board of Adjustment | One public recording is enough to warrant identity investigation; no three-recording minimum for a verified independent body |

Dallas historical committee names include Legislative Affairs, Judicial Nominations, Pensions,
General Investigating/Ethics, Canvassing, COVID Recovery (including human/social and economic
subgroups), Mobility/Sustainability and Human/Social Needs. Reuse an incumbent feed only if official
history proves succession. Otherwise create a named historical parent with an accurate lifecycle.
A generic `AD HOC Committees` or `Joint Board of Directors` recording remains G until identified.

Fort Worth's `T Board` recordings are strong candidates for a Transportation Authority / Trinity
Metro board feed, not City Council. [Official city packet](https://apps.fortworthtexas.gov/OnlinePacketView/ViewDoc.aspx?docid=97)
and [regional transit description](https://www.eulesstx.gov/community/city-information/transportation-services)
support that identity; verify each clip before including it. Fort Worth's numbered Public
Improvement District boards should use **one PID aggregate**, kept separate from TIF; this is a
specific subscription-policy approval within B, not permission to group every advisory board.

Official body evidence also includes [Denton boards](https://www.cityofdenton.com/410/Boards-Commissions-Committees),
[Fort Worth boards](https://www.fortworthtexas.gov/government/boards),
[Dallas Building Inspection](https://dallascityhall.com/government/Boards-and-Commissions/Building-Inspection-Advisory-Examining-Appeals-Boards/Pages/default.aspx)
and [Dallas distinct sign committees](https://dallascityhall.com/departments/pnv/Pages/SPSD.aspx).
These establish named bodies, not the ownership of every abbreviated historical label.

For every approved parent: freeze positive variants and negative neighboring bodies in `evals/`,
create safe union selectors, and register its canonical identity/policy for remedy. Do not make a
new city pay the same alias-review cost: frontier-assisted onboarding resolves the complete archive
and evaluates these rules before regular maintenance starts.

## C — Aggregate time-limited civic processes

Use one city feed for Bond Program Meetings, one for Charter Review, and one for Redistricting
where those families exist. Examples include Dallas 2024 bond subcommittees and input sessions,
Addison Bond Advisory, Denton Bond Oversight / Special Citizens Bond Advisory and charter years,
and Fort Worth bond input / Charter Review / Redistricting Task Force.

Keep program, year and distinct body's name in official titles. Preserve independent parent
identities in evidence even when subscription scope is shared. Redistricting software training is
not a task-force meeting. A construction meeting mentioning a bond can belong to D rather than
oversight; avoid topic-only double inclusion. If an existing family feed exists, expand or migrate
it with aliases rather than create another year-specific feed.

Historical eligibility and polling lifecycle are separate decisions. First ensure the admitted
recordings can be ingested/rendered, then mark officially ended families retired or inactive ones
dormant. Age alone is not proof of termination. Reopen a family subscription when a new program
starts; do not create `bond-2027`, `bond-2030`, etc. Broad family selectors and local remedy policy
must be changed together, with count/date/cross-body regression cases.

## D — Public-input subscriptions instead of hundreds of project one-offs

Dallas district/virtual/telephone town halls, Denton infrastructure/neighborhood virtual meetings,
and Fort Worth neighborhood/project meetings should each have a city Community Public Input
subscription. Keep budgets as a named subfamily only if the city has enough volume and an explicit
subscription preference; the default is the community aggregate.

Use approved format markers, official project lists and reviewed aliases. A street/project name
without `meeting` is a clue requiring recording verification, not an unrestricted neighborhood
selector. Public candidate forums and partner workshops require confirmation that a public-input
session actually occurred and the official archive legitimately hosts it. A bounded fallback of
verified GUIDs is acceptable until a recurring naming pattern is proved.

In future cities this should be an onboarding policy decision with evaluated inclusion/exclusion
rules. Remedy proposes additions to the city policy, never a feed per district, project or date.

## E — Public briefings, with a distinct format boundary

Offer a separate Public Briefings subscription for actual official news conferences, public
reports, state-of-city presentations and civic emergency briefings. This would cover many Dallas
press conferences and Fort Worth COVID updates, including Spanish recordings. Existing Fort Worth
State of City can remain a specific parent or migrate into the aggregate only with approval.

Do not include every short city-news video, weather reminder, promotional update or award ceremony.
Confirm proceedings and use format rules, not `update` or `report` alone. Distinct languages remain
visible in titles; no automatic translation or duplicate UID joins are implied. This category is
optional pending the maintainer's preference about subscription scope.

## F — Explicit exclusions are necessary for the backlog to stay resolved

The maintainer approved excluding promotional videos, ceremonies, staff training and municipal TV
shows from meeting feeds. Examples: Speaker's Podium, Fort Worth Forward, City News/Page Update,
swearing-in ceremonies, concerts and generic commercials. Keep the archive records; exclusion means
no new meeting-feed remediation, not deletion or a retention bypass.

Maintain reviewed, source-scoped exclusion rules and exact ambiguous exceptions in the decision
ledger. The audit consumes those rulings and surfaces a changed/new label that conflicts with them.
A string like `canceled` is not enough to exclude a playable proceeding; those cases remain G.
Test that a real government meeting discussing staff training or a ceremony is not suppressed by
a topic-only keyword. Corpus-level exclusions and validator policy must ship together.

## G — Preserve uncertainty without creating a permanent weekly decision loop

Unclear items include Arlington `Empty`, `fortworthgovMerg`/`fortworthfinal`, bare `CIAC`, canceled/
no-quorum labels, mixed corporate recordings, `The FEED`, `Let's Talk`, and unowned joint boards.
Inspect agendas, canonical recording metadata and public proceedings. Either identify the parent,
verify an exclusion, or admit a real unaffiliated public proceeding to Other Public Meetings by GUID.
Never route an unknown board to Council to make the issue disappear.

G is a finite investigation queue, not a catch-all approval for arbitrary videos. Record the
question, evidence required, prior rejected suggestion and last-reviewed digest. A refusal is
stable until material new evidence arrives. Bundle related identity questions into directional
issues, with at most about twelve active decisions across the queue.

## Completion and implementation sequence

1. Approve directions A-G (and the PID aggregation choice) independently.
2. Refresh source evidence; confirm canonical parents and ambiguous formats. Capture rulings in
   input-only manifests + separate truth under `evals/remedy`, with dated reproducible results.
3. Implement one city/family per PR: selectors, redirects where needed, historical eligibility,
   explicit exclusions and matching remedy policy together. Never split safe feed rules from their
   required policy guard into separately deployable inconsistent changes.
4. Verify every original inventory ID becomes covered, confirmed-excluded or evidence-blocked with
   a specific question. Separately measure legitimate recordings still unserved; an exclusion must
   never count as recovered public-meeting coverage.
5. Only declare historical backlog complete when no legitimate verified recording remains
   unassigned and unresolved identity cases have been adjudicated. Future city onboarding runs this
   same archive-wide flow before ongoing new-label maintenance.

The approval response can be: `A/B/C/D/F approved; E change ...; G investigate; PID approved`.
No approval is inferred from elapsed time or a default option.


## Post-stack reconciliation — 2026-10-03 (Central)

The six-PR stack through #1993 is merged. Build & Deploy run 37168512947 succeeded for
`1822a255f9baf70f2cccc02fb2ee68dc6f8c6196`. A read-only audit restored all 42 configured
source `episodes.json` files and fetched providers using `--dry-run --unexpected-body-evidence`.
It produced zero unexpected-body source bundles and no reported provider failures. It did not
update GitHub, write production state, or dispatch the legacy remedy.

**Zero operational findings does not establish historical coverage.**
`collect_unexpected_bodies` suppresses unmatched labels already present in retained records,
unless a reviewed one-off inclusion uses that label. This explains why #1623's older findings
can disappear after records are archived without their historical feed ownership being resolved.
Do not use this detector as P0 completion evidence or infer an approved exclusion from silence.

A separate replay of all 680 inventory IDs against deployed selectors and the restored archives
found 5 selector-covered labels and 675 labels with retained records lacking a selector match.
The [complete replay](evidence/historical-selector-replay-2026-10-03.csv) retains every inventory ID.

| City | Selector-covered labels | Labels with selector gaps | Unmatched retained UID observations |
|---|---:|---:|---:|
| Addison | 1 | 3 | 11 |
| Arlington | 3 | 0 | 0 |
| Dallas | 0 | 150 | 573 |
| Denton | 0 | 183 | 200 |
| Fort Worth | 0 | 339 | 1,228 |
| Pflugerville | 1 | 0 | 0 |

These are stored UID observations, not unique recordings or newly approved feeds. Cross-view
identities can duplicate a recording; selectors alone do not establish publication eligibility,
media availability, official ownership, or final exclusion. The replay uses each inventory item's
existing source namespace and normalized label, applies current complete source selectors and
exact GUID inclusions, and preserves all archived records. It creates no feed changes.

#1623 remains an operational signal to reconcile with a future controlled audit, while this
inventory remains the historical completion ledger. Preserve held #1986, #1989 and #1991;
publication-selection proof and the P2 evidence/exclusion ledger remain prerequisites for them.
