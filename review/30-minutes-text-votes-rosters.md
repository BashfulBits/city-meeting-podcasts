# review/30 — Minutes Text, Per-item Votes, and Member Rosters

**Maturity: L3 (dev-ready) · ROADMAP R3 extension · 2026-07-14**

This breakout extends [`review/29`](29-agenda-text-extraction.md). It adds no new provider-discovery
mechanism: it consumes provider minutes links and minutes links discovered while extracting agendas.

## Scope

1. Agenda-derived minutes links fill `links["minutes"]` only when that key is absent.
2. Provider-supplied minutes links always take precedence over agenda-derived links.
3. The effective minutes document is extracted by a separate `MinutesTextStage`.
4. Minutes text yields two independent structured sidecars:
   - per-agenda-item member votes;
   - a meeting member/attendance roster for diarization hints.

No vote is inferred from audio. Items without a vote remain absent from the vote artifact.

## Link precedence and matching

Agenda-derived minutes links are associated with the nearest earlier episode in the same source whose
canonical body name matches exactly. A same-body same-day ambiguity is skipped rather than guessed.
The derived link stores provenance in `links["minutes_source"] = "agenda_link"` and
`links["minutes_source_episode_uid"]`. A later provider refresh containing a non-empty canonical
minutes link replaces the derived link and clears the derived provenance.

The agenda/packet crawler follows only HTTPS URLs on the provider's validated host set, deduplicates
links, and applies bounded depth, link-count, response-size, and total-text limits. Per-item links are
retained when the source exposes an item page or anchor; otherwise they remain meeting-level links.

## Artifacts

`Episode` gains independent fields and artifact blocks:

- `minutes_text_url`, `minutes_text_attempts`, `minutes_text_last_attempt`;
- `minutes_votes_url`, `minutes_roster_url`;
- `minutes_text`, `minutes_votes`, and `minutes_roster` artifact blocks;
- `MINUTES_TEXT_PIPELINE_VERSION`, `MINUTES_VOTES_PIPELINE_VERSION`, and
  `MINUTES_ROSTER_PIPELINE_VERSION`.

Each sidecar is content-addressed and includes its source URL, pipeline version, extraction method,
and evidence references. A minutes-text failure never clears a prior successful artifact.

## Vote schema

Votes are per agenda item and contain individual member dispositions:

```json
{
  "agenda_item": "Zoning variance at 412 Main St",
  "chapter_index": 4,
  "votes": [
    {"member": "Alice Smith", "vote": "yes"},
    {"member": "Bob Jones", "vote": "no"},
    {"member": "Carol Lee", "vote": "absent"},
    {"member": "David Patel", "vote": "recused"}
  ],
  "outcome": "passed",
  "evidence": "Motion carried by a vote of 6-1.",
  "source_url": "https://example.gov/minutes.pdf",
  "method": "deterministic"
}
```

`vote` is one of `yes`, `no`, `absent`, `recused`, `abstain`, or `unknown`. Missing information is
represented as `unknown`/`null`, never guessed. Each record carries the exact evidence excerpt and
source URL.

## Roster and diarization

The roster sidecar stores names, roles, attendance status, and evidence spans from the minutes. The
future diarization stage consumes it as a candidate vocabulary and prior, but a name is not asserted
as an audio identity without diarization evidence and the existing identity-confirmation safeguards.

## Extraction policy

The first implementation is deterministic: heading/item association, member-name matching against the
minutes roster, and vote-language parsing. LLM use is optional and non-canonical. A later small-model
fallback may propose a structured parse only when deterministic extraction is ambiguous; it must retain
the source excerpt, mark `method = "llm-assisted"`, and never overwrite official minutes or silently
promote an uncertain vote.

## Phasing and tests

- Phase 1: agenda/packet link union and effective minutes-link precedence.
- Phase 2: `MinutesTextStage` and minutes text sidecar.
- Phase 3: deterministic roster/vote sidecars and preservation tests.
- Phase 4: diarization consumes roster candidates.

Tests cover provider-over-derived precedence, nearest prior same-body matching, ambiguous same-day
matches, idempotent inheritance, link-graph bounds/deduplication, minutes extraction, per-item vote
records, roster evidence, and artifact preservation across scoped audio/transcript writes.
