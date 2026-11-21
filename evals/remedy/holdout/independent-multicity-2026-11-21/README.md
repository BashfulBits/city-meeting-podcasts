# Independent multi-city holdout: evidence candidates

**Status: candidate inventory only; not a frozen holdout, approved truth, or admission evidence.**

Issue #2247 needs independent evidence beyond the existing four-recording Denton holdout. The
113-question varied packet is a regression challenge, not an independent sample. This inventory
starts source collection for a broader holdout; it does not change admissions, production routes,
feeds, quotas, or automatic publication. No model calls are authorized by preparing this inventory.

## Source leads

All links below were retrieved on 2026-11-21 and are official municipality sources. Hashes cover
response bytes retrieved that day; dynamic HTML can change and a hash alone is not an archived
snapshot. These leads have not been matched to Citypods recording UIDs or reviewed for suitability.
Their content is not gold and must not be used to score a model yet.

| Candidate | Official evidence | Potential boundary | SHA-256 of retrieved response |
|---|---|---|---|
| Austin Library Commission, 2025 | [Meeting archive](https://www.austintexas.gov/boards-commissions/meetings/2025_35_1) lists agendas, minutes and audio. | Sparse body and recording identity. Bind an individual meeting and audio first. | `260e25ba0e68f7043a03ab20e9bbc559a2782551eb4d75d4e2f679ea6c08575c` |
| Pflugerville Planning and Zoning Commission, 2025-11-03 | [Meeting detail](https://pflugerville.legistar.com/MeetingDetail.aspx?GUID=CC15EA2A-26A7-4BD0-90B0-473200F21442&ID=1347969&Options=info%7C&Search=) lists final agenda, minutes and video. | Commission identity and overlapping membership. Confirm whether a distinct committee was convened. | `6ae3904c089785485c989026d1c910080a7f657d3c7aa7133bed64f5a92adfc7` |
| Waco Plan Commission, 2025-08-26 | [Official agenda PDF](https://www.waco-texas.com/files/sharedassets/public/v/1/departments/planning-amp-zoning/documents/pcmeetings/2025/08.26.25/agenda.pdf). | Commission identity and work-session versus business-session boundaries. Find and bind a recording. | `fe8dd983e5de8ef3c39051c9f22b8ab85d11bb600b968bce7bc342989e588e1b` |
| McKinney Planning & Zoning Commission, 2025-10-28 | [Official agenda PDF](https://mckinney.legistar.com/View.ashx?GUID=3DE11FE1-0DFD-476D-9DE5-5916DA70C854&ID=1348407&M=A). | New-city transfer; the agenda notes that Council members may attend but Council takes no action. Attendance alone does not prove a joint meeting. Bind the recording and taxonomy first. | `ae590c6ae63e74890b239e50a81c236ea134a59a11351e6b89618843040990bf` |

The city names above do not appear in the canonical 27-case regression manifest or the approved
60-recording varied challenge. This is only a coarse screen: it does **not** establish recording,
source, family or near-duplicate disjointness from every tuned input or prior evaluation.
McKinney is outside the current city configuration and needs a separately documented test taxonomy;
do not fabricate an existing feed for it.

## Work required before use

1. Select a sufficiently diverse set across cities and decision types, including wrong-city and
   district claims, genuinely joint and non-joint proceedings, program-year variants, sparse bodies,
   exclusions and mixed-format labels. These four leads do not cover the contract by themselves.
2. For each candidate, retain immutable official evidence bytes, content hashes, retrieval date,
   bounded spans, original title/date, canonical source URL, provider recording ID and stable UID
   when available. Check actual recording availability, completeness and official feed taxonomy.
3. Inventory every tuned seed and prior model input. Check source, family, event and recording
   overlap, including alternate IDs and near-duplicate labels. Hold any uncertain or overlapping
   case out of the independent split; do not assume that a different date makes it independent.
4. Write answer-free inputs separately from proposed truth. Obtain independent human adjudication
   of official evidence and any unapproved policy, including joint membership and negative claims.
   Retain unknowns instead of guessing. The maintainer must review the candidate-specific truth.
5. Freeze versioned manifest, gold and hashes only after adjudication. Run the existing split
   validator and retain its exact comparison inputs and result. Keep gold out of model requests.
   Evaluate claim support and blind owner selection separately, and report critical errors,
   abstentions, failures and unknown truth without converting failures into successes.

A reviewed dataset and a model's route/identity/effort evidence are separate admission gates. This
inventory qualifies no model and does not itself authorize live calls or change operational limits.
