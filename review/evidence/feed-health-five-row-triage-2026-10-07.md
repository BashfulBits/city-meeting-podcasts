# Five unexpected-feed rows — reviewed dispositions (2026-10-07)

This records the maintainer-approved treatment of the five rows in feed-health issue
[#1623](https://github.com/BashfulBits/city-meeting-podcasts/issues/1623). The report's model output
had timeouts and classification failures; those failures are not treated as evidence. The original
provider records, IDs, titles, source namespaces and any available media remain unchanged.

| City and provider title | Evidence and decision | Feed action |
|---|---|---|
| Addison — `George Herbert Walker Bush Elementary Dedication` (GUID `56020`, UID `235c1c030568dac0`) | A school dedication ceremony, not a meeting of a city body. Its existing evidence case is resolved. | No meeting-feed rule. Preserve the source record. |
| Arlington — `Empty` (UID `a9bfe1b4bb6b6564`, GUID ending `clip_id=1163`) | The official [Council agenda](https://arlingtontx.granicus.com/GeneratedAgendaViewer.php?clip_id=1163&view_id=2) identifies a City Council meeting on September 18, 2012, with Council business. The provider title itself is unusable as a general rule. | Add this exact recording to Arlington Council. Do not create a generic `Empty` rule or rewrite the title. |
| Dallas — `South Dallas Fair Park Opportunity Fund Special Called Meeting` (GUID `402897`) | The public recording identifies the named board and shows it convening; the City calendar identifies its September 21, 2026 meeting. | Add a reusable exact title alias to the board's existing feed. Do not route it to Public Info. |
| Denton — `Animal Shelter Advisory Committee on 2021-03-24 3:00 PM` (GUID `116744`, UID `1ba31685c3061fad`) | This is a real meeting of a standing committee. The cached source contains three distinct recordings: July 23, 2020 (`73990`), October 21, 2020 (`87453`) and March 24, 2021 (`116744`). Denton's [official video archive](https://dentontx.new.swagit.com/views/5) lists later committee dates, generally without video links. No Denton Animal Shelter Advisory Committee feed exists in project `main`. | Do not create a feed or add these recordings in this P0 change. Keep the body visible for future review if more recordings appear; do not describe it as a one-off or as having no recordings. |
| Fort Worth — `fortworthgovMerg fortworthgovMerg` (UID `a3e8a8e4ff1a3007`, GUID ending `clip_id=63`) | The retained provider title is malformed and does not identify a body. No reliable matching agenda or meeting identity was found. | No feed rule. Preserve the record and leave it unmatched. |

## Limits of this update

The two selector additions are local configuration changes, not proof of deployment. The Arlington
addition is a one-recording exception because its provider title is `Empty`; the official agenda
provides exact event identity, while no safe title pattern exists. Dallas uses a reusable title rule.
The other three rows do not produce a feed assignment. Feed-health issue #1623 must remain open until
the deployed audit reflects the dispositions and any deliberately untracked cases have a supported,
durable treatment in the review workflow.

The “do not include,” “keep watching,” “existing-feed addition,” and “new-feed proposal” choices are
recorded as a proposal in [review/51](../51-unexpected-body-remedy-flow.md#proposed-unmatched-meeting-dispositions--for-maintainer-review).
They do not yet define storage fields or implementation behavior.
