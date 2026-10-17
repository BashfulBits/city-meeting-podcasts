# Addison UDC historical committee coverage

**Status: Implemented in PR #1992 (merged 2026-10-03) · FROZEN**
The feed  is deployed in
 and verified by
. Generic exact-selector machinery was shipped in PR #1992.
Community Partnership Committee and public-input migrations are covered separately in PR #2003/#2010.

## Subscription decision and evidence

Create `addison-tx-unified-development-code-advisory-committee` for the distinct Unified Development
Code (UDC) Advisory Committee. Its appointment and membership are documented in the
[official March 25, 2025 Council packet](https://agendas.addisontx.gov/docs/2025/CM/20250325_7396/AGENDApacket__03-25-25_0329_7391.pdf):
twelve appointed resident/business members under Resolution R18-79, with a later vacancy appointment.
The committee is not the Planning and Zoning Commission or Council; those bodies consider the code
through their own proceedings.

Each selected official recording page supplies a committee meeting heading and call-to-order agenda:

| Official recording | Date | Retained UID |
|---|---|---|
| [281190](https://addisontx.new.swagit.com/videos/281190) | 2023-11-15 | `40c0e8e436ae3693` |
| [298761](https://addisontx.new.swagit.com/videos/298761) | 2024-02-29 | `2f83ca433284ad45` |
| [303938](https://addisontx.new.swagit.com/videos/303938) | 2024-04-30 | `4bdafbfb40214fff` |

The pages were retrieved read-only through the guarded HTTP session; no media or live LLM calls.
The November agenda discusses signs/wireless facilities; February administrative procedures;
April administrative procedures and districts/uses. Topic overlap supplies no ownership inference.

## Exact selector and remedy policy

The existing substring selector for the committee's name would also select the
[2021-08-25 UDC Open House](https://addisontx.new.swagit.com/videos/136129).
Use `source.body_exact: [Unified Development Code (UDC) Advisory Committee]`, relying on normalized
complete labels rather than title regexes or GUID-only exceptions. Case, punctuation and duplicated
provider copies normalize; Open House and Council-topic suffixes do not become committee meetings.

`remedy_policy.identity_names` names the reviewed official label. Holding clues include the full
code name and UDC committee abbreviation; they prevent a new raw-title feed without proving that
an unknown event belongs to the committee. A reviewed public-input Open House identity can own its
separate subscription under the exact-owner precedence guard. No broader ownership is pre-approved.
Regression seeds under `evals/remedy/policies/addison-udc/` keep evidence and gold separate; they
are not model admission cases or independent evaluation results.

## Replay and preservation

The read-only provider refresh returned 819 observations; agenda headings were verified on
2026-10-03 (UTC). The retained source archive contains
822 UID records, publicly retrieved at `2026-10-02T22:52:58.535250+00:00`, SHA-256
`e49312dd0bda832421329c04d1490b3c9dcc29aa40de8f8010f9d96a76fa6d1d`.
Their union contains 819 unique provider GUIDs; existing duplicates outside the UDC committee
remain distinct archive observations, not additional meetings.

`tests/fixtures/addison_udc_coverage/archive.json` freezes every provider label/GUID and every
retained UID/body observation. Positive metadata retains the three official dates/titles/links and
stored UIDs. Replay selects exactly three distinct recordings in both sources, without selecting
Open House, other boards, Council code discussions, staff training or promotional titles. None of
the three is currently selected by Council, Planning and Zoning or Town Meetings.

Source URL, author and namespace `abbf5e25e078` stay unchanged. Tests verify the original stored
UIDs against the existing identity calculation. No forced backfill, audio hash or pipeline-version
bump; no archive deletion, source migration, UID override or age-based lifecycle inference.
Historical availability is limited to the provider observations and retained archive actually read.

## Separate decisions held

- UDC Open House (`136129`, retained UID `0057c3dfcacb1f57`) remains covered by the current Town
  Meetings GUID inclusion. Preserve that coverage until its public-input aggregate migration;
  the recording is not treated as a missing committee meeting. Its official page labels an Open
  House without a committee call-to-order agenda; additional content evidence belongs to that
  public-input batch.
- CPC is an official Council subcommittee, but recording `393215` has two dates/two retained UIDs.
  Issue #1991 owns publication identity resolution before a Council selector change. Six stored
  CPC rows represent five recordings, not six distinct meetings.
- Other Town Meetings inclusions and promotional/exclusion cleanup are outside this single-body
  decision. This batch does not declare Addison's city baseline complete.
