# review/37 — Stale-feed lifecycle and provider migration

**Maturity: Frozen — implemented in PRs
[#976](https://github.com/BashfulBits/city-meeting-podcasts/pull/976),
[#977](https://github.com/BashfulBits/city-meeting-podcasts/pull/977),
[#978](https://github.com/BashfulBits/city-meeting-podcasts/pull/978), and
[#990](https://github.com/BashfulBits/city-meeting-podcasts/pull/990) · shipped 2026-07-22 · Phase H / H4
operational hardening · umbrella
[GH#970](https://github.com/BashfulBits/city-meeting-podcasts/issues/970)**

## 1. Problem and outcome

The shipped H4 feed-health reconciler intentionally consolidated all stale findings into one rolling
issue. That reduced issue spam, but it left no per-feed owner, decision, approval, or terminal state:
legitimate recesses, dormant bodies, retired bodies, broken filters, and provider migrations all remain
rows in one issue until every row happens to clear. GH#774 therefore behaves as a permanent warning list
rather than an actionable review queue.

Replace that model with bounded cohort parents and one native GitHub sub-issue per stale-feed incident.
Every child must end in one of two ways: observed recovery after a source/config fix, or a committed
human lifecycle decision. No GitHub-only acknowledgement is durable; lifecycle decisions live in the
applicable feed YAML through a reviewed PR.

The same work closes a pre-existing schema gap: changing `provider` or `source` currently changes
`source_key`, marooning the old append-only record namespace. Provider migrations must preserve stable
RSS episode UIDs and prior artifacts whether the replacement provider copied historical episodes or
starts only at the cutover.

## 2. Locked lifecycle model

Omitting `lifecycle` means `active`. There are exactly four states:

| Status | Poll provider? | Run `stale` check? | `recheck_after` |
|---|---:|---:|---|
| `active` | yes | yes | forbidden |
| `paused` | yes | no before the date; yes on/after it | required |
| `dormant` | yes | no indefinitely | forbidden |
| `retired` | no | no | forbidden |

Example:

```yaml
lifecycle:
  status: paused
  recheck_after: 2026-09-15
  reason: "Documented summer recess"
  evidence_url: https://example.gov/meetings
```

`reason` is required for every non-active state. `evidence_url` is optional but, when present, passes
the same source-URL validation boundary as other config URLs. `paused` and `dormant` continue polling,
so a resumed meeting is captured normally. An expired pause behaves as active for stale evaluation
without requiring a second YAML edit. `retired` renders the persisted append-only archive without a
provider fetch; it never deletes records, feeds, pages, or artifacts.

A dormant feed that publishes again emits one deduplicated `dormant-resumed` operational child asking
whether regular active monitoring should resume. It does not silently rewrite configuration.

There is no `investigating` lifecycle state: an open stale child is unresolved. `expected-gap` is
`paused`. Repairing a filter/view and migrating a source are PR actions, not lifecycle states.

## 3. Stable logical source identity

Add an optional top-level `source_id`:

```yaml
source_id: 4ea6c4b78abc
provider: swagit
source:
  list_url: https://example.new.swagit.com/views/123
```

When absent, `source_key(city)` retains its current `provider + source-minus-body` hash behavior. When
present, the validated `source_id` is the record namespace. At an existing feed's first migration,
the config author sets `source_id` to the feed's current computed source key before changing transport,
so `state/sources/<source_id>/episodes.json` and all append-only history remain in place. Feed views
that intentionally share one record store must share the same `source_id`; conflicting reuse across
unrelated entity/source families fails config validation.

`source_id` changes record ownership only. Persisted records retain their existing audio/transcript
artifact pointers. New content-addressed artifacts may use the new provider path naturally; migration
does not itself bump any pipeline version or force audio/ASR regeneration.

### 3.1 Replacement provider copied historical episodes

The migration dry-run fetches the candidate source, assigns the existing provider-independent UID
(`author + canonical body + date + same-day sequence`), and compares it to the durable archive:

- UID match: fresh official provider fields/links win; existing derived artifacts remain attached.
- no match after the cutover: genuinely new episode.
- no match before the cutover, or multiple plausible matches: ambiguity; the migration cannot apply.
- duplicate UID/provider row: report and deduplicate before apply.

Body renames, shifted dates, or different same-day ordering can defeat the default UID join. Provide a
small explicit, reviewed UID-override mapping as the escape hatch; never guess through ambiguity:

```yaml
uid_overrides:
  replacement-provider-guid: 0123456789abcdef  # existing stable UID from the archive
```

Keys are replacement-provider GUIDs and values must be existing 16-character stable UIDs. Duplicate
targets, absent provider GUIDs, and targets missing from the durable archive fail closed. Run
`python -m citypods.cli migrate-source-report --city <feed-slug> --cutover YYYY-MM-DD` against the
candidate YAML and a restored local state snapshot before merging. The report shows matched history,
new episodes, ambiguities, applied overrides, duplicate projected UIDs, and the projected archive;
its nonzero exit status means the cutover is not ready.

### 3.2 Replacement provider contains only new episodes

The config changes `provider`/`source` while preserving `source_id`. Old records remain in the same
append-only store and continue rendering; new-provider records append as new UIDs. The old provider is
no longer polled. No multi-provider runtime schema is required for v1; introduce concurrent/overlap
polling only after a real migration demonstrates that an atomic cutover is insufficient.

In both cases, the migration path must never delete predecessor state. Any eventually unreferenced
artifacts fall through the existing quarantine/reclaim policy rather than migration-specific deletion.

## 4. GitHub lifecycle

### 4.1 Cohorts and native children

The audit creates a dated stale-review cohort parent only when no open cohort has capacity. Each parent
is capped at 50 native GitHub sub-issues. A child represents one feed incident and contains:

- hidden versioned identity (`stale::<feed-slug>::<incident-id>`);
- first/last observation, newest episode, and inferred cadence;
- official calendar and provider links;
- a direct link to `config/feeds/<slug>.yml` for a manual repair/provider-migration PR;
- prior incident/disposition links; and
- generated-section markers so evidence refresh never overwrites human notes.

Reruns edit evidence only when material details change and comment only on state transitions. Fresh
content safely auto-closes a child even though it started with `needs:human-verification`. A merged
lifecycle PR closes the child once the audit observes the committed disposition. The cohort parent
closes when every child closes. A later recurrence creates a new incident in the then-current cohort;
closed cohorts remain immutable operational history.

### 4.2 Maintainer-only lifecycle commands

Generated child issues accept:

```text
/stale pause --until YYYY-MM-DD --reason "..." [--evidence URL]
/stale dormant --reason "..." [--evidence URL]
/stale retire --reason "..." [--evidence URL]
```

Only collaborator-or-higher actors may invoke them. Parsing is strict and shell-injection-safe. The
workflow resolves the child marker to the exact feed YAML, recreates the requested edit from fresh
`main`, runs config validation/tests, and opens or updates one idempotent maintainer-review PR. It never
pushes to `main`. The command records intent; the merged PR is the approval. Repeated equivalent commands
must not create duplicate branches/PRs.

Source repairs and migrations use an ordinary manual PR from the YAML link in the child. The child
remains open until a later audit passes.

## 5. Implementation slices

1. **[GH#971](https://github.com/BashfulBits/city-meeting-podcasts/issues/971) — stable source identity
   and provider-migration continuity.** `models.City`, `config._build_city`, `records.source_key`, a
   migration comparison/report module + CLI, ambiguity/override validation, and both cutover tests.
2. **[GH#972](https://github.com/BashfulBits/city-meeting-podcasts/issues/972) — lifecycle schema and
   execution.** Parse/validate the four states; apply poll and stale gates; render retired history;
   emit dormant-resumed findings.
3. **[GH#973](https://github.com/BashfulBits/city-meeting-podcasts/issues/973) — bounded native
   sub-issue cohorts (shipped in [PR #977](https://github.com/BashfulBits/city-meeting-podcasts/pull/977)).** Replace consolidated stale reconciliation
   while leaving unrelated check reconciliation unchanged; preserve first-seen history and human text.
   The reconciler indexes marker-owned open and closed incident history, attaches each new child through
   GitHub's native sub-issue API, rolls over at 50 total children, and edits only generated sections.
   Recovery closure requires a conclusive fetch; provider-unreachable and hard-empty active audits leave
   the child open, while a committed lifecycle disposition may close it independently. While the legacy
   marker-owned stale issue remains open, stale cohort creation is gated so #975 can preserve GH#774's
   first-seen history without duplicate incidents; dormant-resumed incidents are not gated.
4. **[GH#974](https://github.com/BashfulBits/city-meeting-podcasts/issues/974) — `/stale` command PR
   automation (shipped in [PR #978](https://github.com/BashfulBits/city-meeting-podcasts/pull/978)).** A deny-by-default workflow admits only
   collaborator-or-higher issue comments, while the Python handler independently verifies the actor,
   open feed-health child label, versioned incident marker, exact feed path, strict command grammar,
   future pause date, reason, and schema-valid HTTPS evidence URL. It edits from fresh `main`, validates the
   full catalog plus focused config/command tests, and creates or updates the deterministic
   `chore/stale-<issue>-lifecycle` branch and one review PR. Equivalent reruns reuse the unchanged remote
   branch; no path pushes to `main`, and issue feedback keeps the incident open pending merge + audit.
5. **[GH#975](https://github.com/BashfulBits/city-meeting-podcasts/issues/975) — production rollout
   (shipped in [PR #990](https://github.com/BashfulBits/city-meeting-podcasts/pull/990)).** A dedicated
   dry-run-first migration validates a one-to-one row/date/config plan, creates and attaches children
   before changing the parent marker, and resumes safely after a partial failure. On 2026-07-22 it
   converted all 11 GH#774 rows to native children #979–#989 with exact `first_seen` preservation,
   feed-YAML links, lifecycle/source guidance, labels, and Operations Project fields. The normal audit
   owns evidence refresh and closure; the children now own human triage, and GH#774 closes when the
   last incident resolves.

Slices 1 and 2 are independent foundations. Slice 3 may build alongside them but cannot close children
from lifecycle state until Slice 2 lands. Slice 4 depends on Slice 2's schema. Slice 5 is last.

## 6. Tests and acceptance

- Legacy configs produce byte-identical `source_key` values when `source_id` is absent.
- A configured `source_id` survives provider and source-URL changes; duplicate/conflicting IDs fail.
- Historical-copy migration reuses UIDs/artifacts; forward-only migration retains old records and
  appends new ones; ambiguous history requires an explicit override.
- Lifecycle validation exhaustively covers status/date/reason combinations.
- Paused/dormant polling, expired-pause stale evaluation, and retired render-without-fetch are tested.
- Child create/update/recovery, native-parent attachment, 50-child rollover, and parent closure are
  tested without live GitHub calls.
- Slash-command actor authorization, strict parsing, URL validation, idempotent PR reuse, and
  command→YAML output are tested.
- GH#774 migration preserves every currently affected slug and its original `first_seen` value.

## 7. Implementation record

PRs #976–#978 shipped the provider-migration, lifecycle, native-incident, and maintainer-command
foundations. PR #990 completed the production rollout and froze this design. The live migration preserved
every legacy GH#774 slug and timestamp, attached children #979–#989 before converting the parent, and
placed the cohort in the Operations Project. Focused tests cover create/update, conclusive recovery,
committed-disposition closure, parent closure, rollover, strict command authorization/parsing, both
provider migration shapes, and interruption-safe legacy conversion.

This changed architecture and operations but not audio bytes. No pipeline version was bumped and no
audio, transcript, feed, or derived-artifact backfill was triggered.
