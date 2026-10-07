# Reopened named-body suggestions from the P0 baseline (2026-10-06)

**Status: the maintainer's cutoff is applied in local feed configuration; none of these changes is
merged or live.**

The earlier catch-all archive direction marked these entries archive-only because no draft committee
definitions had been shown. On 2026-10-07, the maintainer approved drafting the specific names listed
in the prior response. Their reusable source-name selectors now exist in local config (or extend an
existing feed). The final outcomes below supersede earlier family-level “pending” language in this
research worksheet. This does not change production feeds or remove any archive data.

The 680 items are historical title patterns, not 680 unique meetings. This reopened set contains
**211 title patterns in 106 candidate body/family rules, covering 871 source/UID pairs**.
A pair is one provider-view listing; two views can still point to the same video. Some candidate names
have many provider listings even though they share one normalized title pattern. A candidate may be a
standing committee, a time-limited task force, a renamed body, or a joint meeting. Original titles,
source records, UIDs, source namespaces and audio stay unchanged.

## Final maintainer outcome — 2026-10-07

**Superseded by the completed local rule set:** The final source/UID inventory and full replay are in [the Oct. 7 P0 report](p0-forward-rule-replay-2026-10-07.md). The exact per-row unmatched dispositions are in [the unmatched-record CSV](p0-final-unmatched-records-2026-10-07.csv). The numbers below describe the initial reopened proposal set; use the final report for current totals.


The maintainer approved a practical cutoff: create a new named-body feed when the body has at least
five distinct historical UIDs; add exact aliases or joint labels to an existing feed even below five;
mark the remaining small, distinct bodies “not to pursue.” Denton’s Bond Oversight Committee is a
specific earlier exception: the maintainer directly requested its feed when four UIDs were known.

Of the 211 reopened patterns (871 source/UID listings), **180 patterns covering 817 listings** now
have local rule-based feed destinations. The remaining **31 patterns covering 54 listings** are
marked not to pursue. Eight low-count patterns were recognized as additions to existing or newly
approved body feeds rather than new bodies: Fort Worth Housing and Economic Development #1/#2;
the joint Infrastructure and Transportation / Neighborhood Quality meeting; the Ad Hoc Emergency
Medical Response alias; the joint Human Relations / Mayor’s Disabilities meeting; Park Board and
Park Advisory Board aliases; and the older Pedestrian and Bicycle Advisory Committee name. Joint
labels are added to both named participant feeds. These selectors use complete body labels, not UID
inclusions.

The Dallas Arts District Sign Advisory Committee remains outside the existing Special Sign District
feed: the City lists them as separate committees, so similar wording is not enough to combine them.
All current assignments and preserve-in-archive decisions remain local drafts until they are merged,
replayed across the cached catalog, and verified against live feeds.

## Denton bond suggestions

### Approved local feed drafts

The following source-wide definitions were requested on 2026-10-07. New feeds use reusable name
selectors, not recording-ID lists: Fort Worth Housing and Economic Development, Neighborhood Quality
and Revitalization, Entrepreneurship and Innovation, and the exact observed T Board labels; Dallas
Youth Commission, Mobility Solutions/Infrastructure/Sustainability, and Human and Social Needs; and
Denton Committee on the Environment, Development Code Review, Public Art, Airport Advisory Board,
Bond Oversight, Library Board, and Special Citizens Bond Advisory Committee. The Library Board uses
the reusable title rule for its own meetings and retains the one-off provider-ID inclusion for the
verified Council/Library joint recording; Council also keeps its participant assignment.

Some historical meetings still need exact agenda-to-recording checks before their case can be closed.
The Fort Worth Housing name rule also matches numbered title variants, and Neighborhood Quality can
match a joint title; keep these visible for event verification and participant-feed follow-up. The
T Board labels are restricted to the two exact observed complete names pending evidence about dated
variants. Cancellation and no-quorum titles are not special-cased in these selectors; the existing
no-recording flow handles items without usable recordings. These configurations are local drafts,
not deployed feeds.

### Special Citizens Bond Advisory Committee — 14 title patterns

This is a different body from Denton’s continuing Bond Oversight Committee. The 14 patterns cover
the 2019 and 2023 bond-election advisory cycles. My recommended rule is to reuse the existing
Special Citizens Bond Advisory Committee feed and match the full named-body phrase on Denton’s
Swagit source. That catches a similar future citizens committee when Denton runs another bond
program without a list of recording IDs. Keep the program year in the original official title. This
needs a source-wide replay and confirmation that every dated provider entry matches its City agenda.
The existing feed previously included only the June 6, 2019 recording; its full-name selector is now
drafted. Source-wide replay and confirmation that each dated provider entry matches its City agenda
remain required before closing these cases.

Examples: “Special Citizens Bond Advisory Committee on 2019-06-06…”, “...2019-06-27…”,
“...2019-07-11…”, “...2023-05-23…”, “...2023-06-26…”, and “...2023-07-11…”.
The user’s earlier “exclude the rest” instruction caused these to be marked archive-only; they are now
reopened as a suggestion, not treated as a final exclusion.

### Bond Oversight Committee — 4 title patterns

This is the continuing Denton committee that oversees voter-approved bond programs. Its feed
definition exists in the working tree: on Denton’s Swagit source, match the complete phrase
“Bond Oversight Committee.” It handles dates, amendments and special-called meeting suffixes while
leaving the original title intact. It does not match Special Citizens Bond Advisory Committee, generic
bond updates or Council discussions. The selector matches exactly the four historical patterns. The
feed is not merged or deployed yet. The City describes the committee at
[Bond Oversight Committee](https://www.cityofdenton.com/415/Bond-Oversight-Committee).

## Other reopened body and committee suggestions

The lines below show the candidate destination, the reusable matching idea, sample historical titles,
and what proof remains. Counts are title patterns and source/UID pairs; two provider views can show the
same recording. “Candidate” means the city body or family should be checked, not that every record is
already proven to belong there. Within each city, families with more source/UID listings are shown first.

### Denton

- **30 patterns / 30 source/UID pairs — `denton-tx-committee-on-the-environment`.** Match “Committee on the Environment” after removing only the embedded “on YYYY-MM-DD time” suffix; keep official recording title/date and all source identities unchanged.
  Sample titles: Committee on the Environment on 2020-10-05 9:00 AM; Committee on the Environment on 2015-08-03 1:30 PM.
  Evidence status: Official institution and archived-meeting evidence checked; reusable name rule supported, exact feed application still pending.
- **20 patterns / 20 source/UID pairs — `denton-tx-development-code-review-committee`.** Match the exact official body name “Development Code Review Committee” while ignoring only the embedded meeting date/time and special-called suffix; do not broaden to all development-code items.
  Sample titles: Development Code Review Committee on 2018-10-26 11:00 AM; Development Code Review Committee on 2018-08-10 11:00 AM.
  Evidence status: Official institution and archived-meeting evidence checked; reusable name rule supported, exact feed application still pending.
- **16 patterns / 16 source/UID pairs — `denton-tx-public-art-committee`.** Match the exact Public Art Committee body name and remove only meeting date/time or special-called suffixes; do not match general art events or public-art project names.
  Sample titles: Public Art Committee on 2020-09-04 3:00 PM; Public Art Committee on 2018-09-06 4:00 PM.
  Evidence status: Official institution and archived-meeting evidence checked; reusable name rule supported, exact feed application still pending.
- **8 patterns / 8 source/UID pairs — `denton-tx-airport-advisory-board`.** Dedicated Denton Airport Advisory Board feed; normalize only date/time and “special called meeting” suffixes.
  Sample titles: Airport Advisory Board on 2021-03-11 1:00 PM; Airport Advisory Board on 2021-04-08 1:30 PM.
  Evidence status: Strong exact-body rule candidate; source/player replay remains
- **6 patterns / 6 source/UID pairs — `denton-tx-hotel-occupancy-tax-committee`.** Treat the old Denton Hotel Occupancy Tax and Sponsorship Committee name as a dated alias for today’s Community Partnership Committee, using exact body labels only.
  Sample titles: Hotel Occupancy Tax Committee on 2018-06-21 9:00 AM; Hotel Occupancy Tax Committee on 2016-05-03 10:00 AM.
  Evidence status: Strong historical-alias rule; replay both old and new exact names
- **4 patterns / 4 source/UID pairs — `denton-tx-library-board`.** Dedicated Denton Library Board feed, matching the exact board name and stripping only date/time.
  Sample titles: Library Board on 2021-03-11 3:30 PM; Library Board on 2021-02-11 2:00 PM.
  Evidence status: Strong exact-body rule candidate; verify each recording link
- **3 patterns / 3 source/UID pairs — `denton-tx-animal-shelter-advisory-committee`.** Dedicated Denton Animal Shelter Advisory Committee feed; exact committee name, strip date/time only.
  Sample titles: Animal Shelter Advisory Committee on 2020-10-21 3:00 PM; Animal Shelter Advisory Committee on 2021-03-24 3:00 PM.
  Evidence status: Strong exact-body rule candidate; source/recording replay remains
- **3 patterns / 3 source/UID pairs — `denton-tx-hotel-occupancy-tax-and-sponsorship-committee`.** Use a historic exact-name alias: “Hotel Occupancy Tax and Sponsorship Committee” maps to Denton’s later Community Partnership Committee.
  Sample titles: Hotel Occupancy Tax and Sponsorship Committee on 2020-05-14 1:00 PM; Hotel Occupancy Tax and Sponsorship Committee on 2020-07-20 9:00 AM.
  Evidence status: Strong continuity/name-change rule supported
- **2 patterns / 2 source/UID pairs — `denton-tx-community-partnership-committee`.** Dedicated Denton Community Partnership Committee feed. Match the exact current body name and strip only date/time. Keep pre-2020 predecessor titles as the historical alias described at rank 22.
  Sample titles: Community Partnership Committee on 2021-03-18 9:00 AM; Community Partnership Committee on 2021-04-22 9:00 AM.
  Evidence status: Strong exact-body plus historical alias rule supported
- **1 patterns / 1 source/UID pairs — `denton-tx-capital-improvement-advisory-committee`.** Match the source-scoped official body name 'Capital Improvement Advisory Committee on 2025-06-25 4:00 PM', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Capital Improvement Advisory Committee on 2025-06-25 4:00 PM.
  Evidence status: Preliminary title-only proposal; official evidence still needed

### Dallas

- **1 patterns / 49 source/UID pairs — `dallas-tx-youth-commission`.** Exact Dallas Youth Commission rule, preserving dated meeting titles.
  Sample titles: Youth Commission.
  Evidence status: Strong named-body candidate; bind historical clips to dates.
- **1 patterns / 34 source/UID pairs — `dallas-tx-mobility-solutions-infrastructure-and-sustainability-committee`.** Exact Dallas Mobility Solutions, Infrastructure and Sustainability Committee rule; allow only official shortened display-name forms.
  Sample titles: Mobility Solutions, Infrastructure and Sustainability Committee.
  Evidence status: Strong named-body candidate; validate historic clips and any joint labels.
- **1 patterns / 33 source/UID pairs — `dallas-tx-human-social-needs-committee`.** Exact Dallas Human and Social Needs Committee rule, allowing the official ampersand spelling.
  Sample titles: Human & Social Needs Committee.
  Evidence status: Strong named-body candidate; replay dated recordings.
- **1 patterns / 17 source/UID pairs — `dallas-tx-ad-hoc-committee-on-general-investigating-and-ethics`.** Dallas-source exact-name rule for “Ad Hoc Committee on General Investigating and Ethics,” removing only a dated meeting suffix. Do not match generic “ethics” or “investigating.” Re-check committee appointment/term across years; identical name alone does not prove unchanged membership or legal continuity.
  Sample titles: Ad Hoc Committee on General Investigating and Ethics.
  Evidence status: Official Dallas committee records confirm the exact named body and recurring dated meetings; verify each archived UID against its dated committee agenda before applying.
- **1 patterns / 16 source/UID pairs — `dallas-tx-legislative-ad-hoc-committee`.** Exact Dallas Legislative Ad Hoc Committee rule.
  Sample titles: Legislative Ad Hoc Committee.
  Evidence status: Strong named-body candidate; verify archive/player matches.
- **1 patterns / 14 source/UID pairs — `dallas-tx-ad-hoc-committee-on-legislative-affairs`.** Use one exact-name feed for this Dallas Council committee across its dated appearances. Keep the date and official label on each item. This is a recurring named committee series, not proof of one uninterrupted appointment or roster.
  Sample titles: Ad Hoc Committee on Legislative Affairs.
  Evidence status: Body/rule evidence supported; per-record source-to-agenda matching remains open.
- **1 patterns / 13 source/UID pairs — `dallas-tx-ad-hoc-committee-on-covid-19-recovery-assistance`.** Match the full 2022 committee name and its documented date/name variants only. Keep it separate from the earlier Council Ad Hoc Committee on COVID-19 Human and Social Recovery and Assistance.
  Sample titles: Ad Hoc Committee on COVID-19 Recovery & Assistance.
  Evidence status: Body/rule evidence supported; per-record source-to-agenda matching remains open.
- **1 patterns / 12 source/UID pairs — `dallas-tx-council-ad-hoc-committee-on-covid-19-human-and-social-recovery-and-assistance`.** Match only the full 2020 Human and Social Recovery committee name. Do not merge it with the later 2022 Ad Hoc Committee on COVID-19 Recovery & Assistance.
  Sample titles: Council Ad Hoc Committee on COVID-19 Human and Social Recovery and Assistance.
  Evidence status: Body/rule evidence supported; per-record source-to-agenda matching remains open.
- **1 patterns / 11 source/UID pairs — `dallas-tx-automated-red-light-commission`.** Exact Dallas Automated Red Light Enforcement Commission rule, preserving the official acronym only as an alias.
  Sample titles: Automated Red Light Commission.
  Evidence status: Strong named-body candidate; verify exact recording links.
- **1 patterns / 11 source/UID pairs — `dallas-tx-commission-on-homelessness`.** Use an exact, source-scoped official body name and documented aliases. Keep a later body separate unless a City action explicitly establishes succession; require dated recording matches before routing.
  Sample titles: Commission on Homelessness.
  Evidence status: Evidence hold on alias/continuity; maintain historical title candidate.
- **1 patterns / 10 source/UID pairs — `dallas-tx-ad-hoc-committee-on-pensions`.** Match only the full Pensions committee name within the documented 2023–24 work period. Keep pension-related Council briefings and other retirement-policy recordings separate.
  Sample titles: Ad Hoc Committee on Pensions.
  Evidence status: Body/rule evidence supported; per-record source-to-agenda matching remains open.
- **1 patterns / 8 source/UID pairs — `dallas-tx-ad-hoc-judicial-nominating-committee`.** Exact Dallas Ad Hoc Judicial Nominating Committee rule.
  Sample titles: Ad Hoc Judicial Nominating Committee.
  Evidence status: Candidate; verify notice and exact recording identity.
- **1 patterns / 7 source/UID pairs — `dallas-tx-ad-hoc-city-council-canvassing-committee`.** Match only official Ad Hoc City Council Canvassing Committee recordings whose election date and committee roster appear in the City Secretary’s election records. Keep each election cycle’s original title; do not match canvassing topics elsewhere.
  Sample titles: Ad Hoc City Council Canvassing Committee.
  Evidence status: Official institutional evidence checked; exact historical recording links still need binding
- **1 patterns / 7 source/UID pairs — `dallas-tx-council-ad-hoc-committee-on-covid-19-economic-recovery-and-assistance`.** Match only the official source-scoped body name 'Council Ad Hoc Committee on COVID-19 Economic Recovery and Assistance', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Council Ad Hoc Committee on COVID-19 Economic Recovery and Assistance.
  Evidence status: Official evidence found; bind the exact dated recording before applying.
- **1 patterns / 7 source/UID pairs — `dallas-tx-council-ad-hoc-committee-on-covid-19-recovery-and-assistance`.** Match only the official source-scoped body name 'Council Ad Hoc Committee on COVID-19 Recovery and Assistance', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Council Ad Hoc Committee on COVID-19 Recovery and Assistance.
  Evidence status: Unresolved: source identity or meeting type still needs official evidence.
- **1 patterns / 5 source/UID pairs — `dallas-tx-mayor-s-task-force-on-confederate-monuments`.** Match only the official source-scoped body name 'Mayor's Task Force on Confederate Monuments', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Mayor's Task Force on Confederate Monuments.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 4 source/UID pairs — `dallas-tx-joint-education-task-force`.** Match only the official source-scoped body name 'Joint Education Task Force', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Joint Education Task Force.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 2 source/UID pairs — `dallas-tx-arts-district-sign-advisory-committee`.** Match the exact Arts District Sign Advisory Committee name and official shortened form ADSAC. Do not merge it with the separately named Special Sign District Advisory Committee.
  Sample titles: Arts District Sign Advisory Committee.
  Evidence status: Official body/term evidence checked; bind exact historical recordings
- **1 patterns / 2 source/UID pairs — `dallas-tx-building-inspection-advisory-examining-and-appeals-board-special-called-meeting`.** Match official Building Inspection Advisory, Examining and Appeals Board labels and special-called suffixes only after confirming the City charter/roster treats them as one body.
  Sample titles: Building Inspection Advisory, Examining and Appeals Board Special Called Meeting.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 2 source/UID pairs — `dallas-tx-building-inspection-advisory-examining-appeals-boards`.** Match official Building Inspection Advisory, Examining and Appeals Board labels and special-called suffixes only after confirming the City charter/roster treats them as one body.
  Sample titles: Building Inspection Advisory, Examining & Appeals Boards.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **2 patterns / 2 source/UID pairs — `dallas-tx-city-planning-commission`.** Separate a City Plan Commission field-tour recording from the Dallas Zoning Ordinance Advisory Committee. Use exact body labels; do not merge a bus tour into the ZOAC feed.
  Sample titles: CPC Bus Tour; Zoning Ordinance Advisory Committee.
  Evidence status: Two different source families; exact recording binding remains
- **1 patterns / 2 source/UID pairs — `dallas-tx-comprehensive-land-use-plan-committee`.** Match the exact CLUP Committee title and documented acronym; keep community workshops and public hearings separate from committee proceedings.
  Sample titles: Comprehensive Land Use Plan Committee.
  Evidence status: Conflicting evidence: official CLUP committee dates begin in 2023, but this inventory title is dated 2022; resolve date/source identity before routing.
- **1 patterns / 2 source/UID pairs — `dallas-tx-council-agenda`.** Route only a confirmed full Council session to the Council feed; do not automatically publish agenda documents as recordings.
  Sample titles: Council Agenda.
  Evidence status: Unresolved: source identity or meeting type still needs official evidence.
- **1 patterns / 2 source/UID pairs — `dallas-tx-mayor-s-task-force-on-poverty`.** Match only the official source-scoped body name 'Mayor’s Task Force on Poverty', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Mayor’s Task Force on Poverty.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 2 source/UID pairs — `dallas-tx-subdivision-review-committee`.** Match only the official source-scoped body name 'Subdivision Review Committee', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Subdivision Review Committee.
  Evidence status: Title suggests a committee; official identity and meeting type still need specific evidence.
- **1 patterns / 2 source/UID pairs — `dallas-tx-trinity-parkway-advisory-committee`.** Match only the official source-scoped body name 'Trinity Parkway Advisory Committee', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Trinity Parkway Advisory Committee.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **2 patterns / 2 source/UID pairs — `dallas-tx-urban-design-peer-review-board`.** Dedicated Dallas Urban Design Peer Review Panel feed, using exact panel aliases only.
  Sample titles: Urban Design Peer Review Panel; Urban Design Peer Review.
  Evidence status: Strong exact-body rule candidate; per-record video binding
- **1 patterns / 1 source/UID pairs — `dallas-tx-ad-hoc-committee-on-judicial-nominations`.** Match the source-scoped official body name 'Ad Hoc Committee on Judicial Nominations', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Ad Hoc Committee on Judicial Nominations.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-ad-hoc-committees`.** Match the source-scoped official body name 'AD HOC Committees', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: AD HOC Committees.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-ad-hoc-judical-nominations-committee`.** Match the source-scoped official body name 'AD HOC Judical Nominations Committee', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: AD HOC Judical Nominations Committee.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-committee-on-homeland-security-joint-hearing`.** Match the source-scoped official body name 'Committee on Homeland Security Joint Hearing', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Committee on Homeland Security Joint Hearing.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-education-task-force`.** Match the source-scoped official body name 'Education Task Force', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Education Task Force.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-joint-board-of-directors`.** Match the source-scoped official body name 'Joint Board of Directors', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Joint Board of Directors.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-mayor-s-poverty-task-force`.** Match the source-scoped official body name 'Mayor's Poverty Task Force', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Mayor's Poverty Task Force.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-special-called-joint-meeting-ad-hoc-and-gpfm-committee`.** Match the source-scoped official body name 'Special Called Joint Meeting AD Hoc and GPFM Committee', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Special Called Joint Meeting AD Hoc and GPFM Committee.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `dallas-tx-texas-house-committee-on-land-and-resource-management-meeting`.** Match the source-scoped official body name 'Texas House Committee on Land and Resource Management Meeting', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Texas House Committee on Land and Resource Management Meeting.
  Evidence status: Preliminary title-only proposal; official evidence still needed

### Fort Worth

- **1 patterns / 82 source/UID pairs — `fort-worth-tx-housing-and-economic-development-committee`.** Match the exact historical “Housing and Economic Development Committee” body name (case-insensitive, with confirmed date/time suffixes only) for Fort Worth records dated through its 2015 retirement. Do not extend the match to later Housing and Neighborhood Services Committee meetings without official continuity evidence.
  Sample titles: HOUSING AND ECONOMIC DEVELOPMENT COMMITTEE HOUSING AND ECONOMIC DEVELOPMENT COMMITTEE.
  Evidence status: Official committee identity and 2015 end of standing status verified; bind all 80 archived observations to dated agendas/players
- **1 patterns / 46 source/UID pairs — `fort-worth-tx-neighborhood-quality-and-revitalization-committee`.** Exact Fort Worth Neighborhood Quality and Revitalization Committee rule.
  Sample titles: Neighborhood Quality and Revitalization Committee Neighborhood Quality and Revitalization Committee.
  Evidence status: Strong exact-body candidate; verify all dated recordings.
- **1 patterns / 38 source/UID pairs — `fort-worth-tx-t-board-meeting`.** If the dated City-hosted source confirms a Board meeting, map exact historical “T Board Meeting” title forms from 2010–2013 to a separate Trinity Metro Board of Directors feed; preserve historical name/date and source namespace. Do not match the single letter T or general transit topics.
  Sample titles: T Board Meeting T Board Meeting.
  Evidence status: Official Trinity Metro Board identity confirmed; bind all 37 City-archive observations to dated board agendas
- **1 patterns / 34 source/UID pairs — `fort-worth-tx-entrepreneurship-and-innovation-committee`.** Exact Fort Worth Entrepreneurship and Innovation Committee rule.
  Sample titles: Entrepreneurship and Innovation Committee Entrepreneurship and Innovation Committee.
  Evidence status: Strong exact-body candidate; bind observations to official agenda/video.
- **6 patterns / 28 source/UID pairs — `fort-worth-tx-public-art-commission`.** Dedicated Fort Worth Art Commission feed. Match “Art Commission” and “Fort Worth Art Commission” as aliases; ignore only special-called/date wording.
  Sample titles: Fort Worth Art Commission Special Meeting Fort Worth Art Commission Special Meeting; Special Called Art Commission Meeting Special Called Art Commission Meeting.
  Evidence status: Strong exact-body rule candidate; confirm each archive recording
- **1 patterns / 24 source/UID pairs — `fort-worth-tx-race-and-culture-task-force`.** A City-created temporary task force with a formal resolution, named mandate and archived proceedings may be recognized as one finite historical body family. Do not include sponsored outreach or Council briefings just because they mention the task force.
  Sample titles: Race and Culture Task Force Race and Culture Task Force.
  Evidence status: Official City resolution and final report establish the finite body and mandate; exact recordings fit a source-scoped task-force rule, with outreach and Council reports kept separate.
- **1 patterns / 22 source/UID pairs — `fort-worth-tx-human-relations-commission`.** Exact Fort Worth Human Relations Commission rule.
  Sample titles: Human Relations Commission Human Relations Commission.
  Evidence status: Strong named-body candidate; confirm event identity and joint participants.
- **1 patterns / 18 source/UID pairs — `fort-worth-tx-mayor-s-committee-on-persons-with-disabilities`.** Exact Fort Worth Mayor’s Committee on Persons with Disabilities rule.
  Sample titles: Mayor's Committee on Persons with Disabilities Mayor's Committee on Persons with Disabilities.
  Evidence status: Strong named-body candidate; verify dates and any joint meetings.
- **1 patterns / 16 source/UID pairs — `fort-worth-tx-park-and-recreation-advisory-board`.** Fort Worth Park and Recreation Advisory Board rule with official historical-name aliases.
  Sample titles: Park and Recreation Advisory Board Park and Recreation Advisory Board.
  Evidence status: Strong historic-body rule candidate; confirm dates and board transition.
- **1 patterns / 14 source/UID pairs — `fort-worth-tx-ad-hoc-council-committee-on-emergency-medical-response`.** One bounded Fort Worth Ad Hoc Council Committee on Emergency Medical Response feed.
  Sample titles: Ad Hoc Council Committee On Emergency Medical Response Ad Hoc Council Committee On Emergency Medical Response.
  Evidence status: Candidate rule supported; exact dated source-to-player bindings remain open.
- **1 patterns / 10 source/UID pairs — `fort-worth-tx-library-advisory-board`.** Exact Fort Worth Library Advisory Board rule with documented board-name aliases only.
  Sample titles: Library Advisory Board Library Advisory Board.
  Evidence status: Strong named-body candidate; bind historical clips.
- **1 patterns / 10 source/UID pairs — `fort-worth-tx-pedestrian-and-bicycle-advisory-commission`.** Use the exact official Commission name and evidence-backed aliases within Fort Worth’s source. Require the meeting date/source to agree; never route every transportation-topic meeting to this body.
  Sample titles: Pedestrian and Bicycle Advisory Commission Pedestrian and Bicycle Advisory Commission.
  Evidence status: Candidate; verify commission authority and dated records.
- **1 patterns / 8 source/UID pairs — `fort-worth-tx-ad-hoc-municipal-court-advisory-committee`.** Match the exact historical “Ad Hoc Municipal Court Advisory Committee” name and documented title variants. Preserve “Ad Hoc” and date; do not merge it with Municipal Court hearings or Council votes.
  Sample titles: Ad Hoc Municipal Court Advisory Committee Ad Hoc Municipal Court Advisory Committee.
  Evidence status: Official body/event identity supports a candidate; exact source/player matches remain a pre-implementation check.
- **1 patterns / 8 source/UID pairs — `fort-worth-tx-minority-and-women-business-enterprise-advisory-committee`.** Match the exact historical “Minority and Women Business Enterprise Advisory Committee” name and documented M/WBE-AC short form through its 2021 restructuring. Do not infer a match from M/WBE subject matter alone.
  Sample titles: Minority and Women Business Enterprise Advisory Committee Minority and Women Business Enterprise Advisory Committee.
  Evidence status: Official body/event identity supports a candidate; exact source/player matches remain a pre-implementation check.
- **1 patterns / 7 source/UID pairs — `fort-worth-tx-aviation-advisory-board`.** Match the official Aviation Advisory Board name and documented date/special-meeting suffixes only. Keep airport authority boards separate.
  Sample titles: Aviation Advisory Board Aviation Advisory Board.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **2 patterns / 6 source/UID pairs — `fort-worth-tx-animal-shelter-advisory-committee`.** Dedicated Fort Worth Animal Shelter Advisory Committee feed; exact body name with date suffix removed.
  Sample titles: Animal Shelter Advisory Committee of October 14, 2020 Animal Shelter Advisory Committee of October 14, 2020; Animal Shelter Advisory Committee Animal Shelter Advisory Committee.
  Evidence status: Strong exact-body candidate; exact agenda binding needed
- **1 patterns / 5 source/UID pairs — `fort-worth-tx-arts-funding-task-force`.** Match only the official source-scoped body name 'ARTS FUNDING TASK FORCE ARTS FUNDING TASK FORCE', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: ARTS FUNDING TASK FORCE ARTS FUNDING TASK FORCE.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 5 source/UID pairs — `fort-worth-tx-capital-improvements-advisory-committee`.** Keep Transportation Impact Fee CIAC separate from Water/Sewer CIAC. Do not use acronym-only matching; add a reusable rule only where the source title/agenda names the program.
  Sample titles: Capital Improvements Advisory Committee Capital Improvements Advisory Committee.
  Evidence status: Evidence hold on ambiguous CIAC identity
- **1 patterns / 4 source/UID pairs — `fort-worth-tx-ad-hoc-committee-on-emergency-medical-response`.** Match only the official source-scoped body name 'Ad Hoc Committee on Emergency Medical Response Ad Hoc Committee on Emergency Medical Response', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Ad Hoc Committee on Emergency Medical Response Ad Hoc Committee on Emergency Medical Response.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 4 source/UID pairs — `fort-worth-tx-allianceairport-authority-inc`.** Match the full official Alliance Airport Authority name and documented corporate suffix variants only after verifying the City/authority agenda and recording.
  Sample titles: AllianceAirport Authority, Inc. AllianceAirport Authority, Inc..
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **2 patterns / 4 source/UID pairs — `fort-worth-tx-bond-program-meetings`.** Create a City bond public-meeting title family only for official bond-election public meetings where notice/agenda confirms resident participation.
  Sample titles: Bond Election Public Meeting Bond Election Public Meeting; Bond Public Input Meeting Bond Public Input Meeting.
  Evidence status: Community Public Input candidate; exact event/source proof needed
- **1 patterns / 4 source/UID pairs — `fort-worth-tx-community-action-partners-council`.** Match only the official source-scoped body name 'Community Action Partners Council Community Action Partners Council', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Community Action Partners Council Community Action Partners Council.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 4 source/UID pairs — `fort-worth-tx-fort-worth-public-improvement-district-no-1-advisory-board`.** Match only the official source-scoped body name 'Fort Worth Public Improvement District No. 1 Advisory Board Fort Worth Public Improvement District No. 1 Advisory Board', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Fort Worth Public Improvement District No. 1 Advisory Board Fort Worth Public Improvement District No. 1 Advisory Board.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 4 source/UID pairs — `fort-worth-tx-fort-worth-sports-authority`.** Match only the official source-scoped body name 'Fort Worth Sports Authority Fort Worth Sports Authority', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Fort Worth Sports Authority Fort Worth Sports Authority.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 4 source/UID pairs — `fort-worth-tx-t-board-of-directors`.** Require an official TRVA/T Board notice and exact archive binding; retain its independent source namespace and do not combine it with Council meetings.
  Sample titles: T Board of Directors T Board of Directors.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 4 source/UID pairs — `fort-worth-tx-water-and-wastewater-capital-improvements-plans-citizen-advisory-committee`.** Match the exact Water/Wastewater Capital Improvements Plan Citizens Advisory Committee family and documented “CIPAC” short form; preserve dates and distinguish it from the Transportation Impact Fee CIAC.
  Sample titles: Water and Wastewater Capital Improvements Plans Citizen Advisory Committee Water and Wastewater Capital Improvements Plans Citizen Advisory Committee.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-ad-hoc-council-committee-on-emergency-medical-response-meeting`.** Match only the official source-scoped body name 'Ad Hoc Council Committee on Emergency Medical Response Meeting Ad Hoc Council Committee on Emergency Medical Response Meeting', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Ad Hoc Council Committee on Emergency Medical Response Meeting Ad Hoc Council Committee on Emergency Medical Response Meeting.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-alliance-airport-authority`.** Match the full official Alliance Airport Authority name and documented corporate suffix variants only after verifying the City/authority agenda and recording.
  Sample titles: Alliance Airport Authority January 12, 2021 Alliance Airport Authority January 12, 2021.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-business-equity-advisory-board-meeting`.** Use distinct exact-body rules unless City ordinances or appointment records establish a rename; do not infer alias from similar business-equity wording.
  Sample titles: Business Equity Advisory Board Meeting Business Equity Advisory Board Meeting.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-business-equity-committee`.** Use distinct exact-body rules unless City ordinances or appointment records establish a rename; do not infer alias from similar business-equity wording.
  Sample titles: Business Equity Committee Business Equity Committee.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-civil-service-commission`.** Match only the official source-scoped body name 'Civil Service Commission Civil Service Commission', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Civil Service Commission Civil Service Commission.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-community-action-partners-cap-council`.** Match only the official source-scoped body name 'Community Action Partners (CAP) Council Community Action Partners (CAP) Council', allowing documented spelling/punctuation and date/time variants. Do not match related topics, separate bodies, or joint proceedings without participant proof.
  Sample titles: Community Action Partners (CAP) Council Community Action Partners (CAP) Council.
  Evidence status: Official body or event family identified; exact dated agenda-to-recording binding still open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-community-action-partners-council-meeting-of-12-17-20-community-action-partners-council-meeting-of`.** Match the exact CAP Council name and date; do not match service/application events. Verify the Dec. 17, 2020 agenda and clip.
  Sample titles: Community Action Partners Council Meeting of 12-17-20 Community Action Partners Council Meeting of.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-deferred-compensation-oversight-committee`.** Match the exact committee name and official date variants only; keep it separate from City pension policy briefings.
  Sample titles: Deferred Compensation Oversight Committee Deferred Compensation Oversight Committee.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-fort-worth-human-relations-commission`.** Use the official commission name and documented aliases only. Preserve joint recordings in both participant feeds when the dated notice confirms both bodies.
  Sample titles: Fort Worth Human Relations Commission Fort Worth Human Relations Commission.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-fort-worth-public-library-advisory-board-meeting`.** Match only the Library Advisory Board title and documented board-name aliases; do not match library events.
  Sample titles: Fort Worth Public Library Advisory Board Meeting Fort Worth Public Library Advisory Board Meeting.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-fort-worth-sports-authority-inc-quarterly-board-meeting`.** Treat ‘Fort Worth Sports Authority’ and ‘FW Sports Authority, Inc.’ as aliases only after authority records confirm the same corporation; normalize quarter/date wording without rewriting displayed titles.
  Sample titles: Fort Worth Sports Authority, Inc. Quarterly Board Meeting, August 25, 2020 Fort Worth Sports Authority, Inc. Quarterly Board Meeting, August 25, 2020.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-fort-worth-water-department-wholesale-water-and-wastewater-advisory-committee`.** Match this full City committee name only; do not route general water-utility meetings or the separate Water/Sewer CIAC here.
  Sample titles: Fort Worth Water Department Wholesale Water and Wastewater Advisory Committee Fort Worth Water Department Wholesale Water and Wastewater Advisory Committee.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-fw-sports-authority-inc`.** Use ‘FW Sports Authority, Inc.’ only as a verified abbreviation of ‘Fort Worth Sports Authority, Inc.’; preserve source title and each board date.
  Sample titles: FW Sports Authority, Inc. FW Sports Authority, Inc..
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-fw-sports-authority-inc-quarterly-board-meeting`.** Match full/abbreviated authority names and the quarterly board-meeting suffix; keep each date as a separate episode.
  Sample titles: FW Sports Authority, Inc. Quarterly Board Meeting May 26, 2020 FW Sports Authority, Inc. Quarterly Board Meeting May 26, 2020.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-housing-neighborhood-services-committee`.** Match only this official committee name and documented spelling variants; do not match general housing services topics.
  Sample titles: Housing Neighborhood Services Committee Housing Neighborhood Services Committee.
  Evidence status: Unresolved: source identity or meeting type still needs official evidence.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-human-relations-commission-mayor-s-committee-on-persons-with-disabilities`.** Do not collapse the two bodies. For a verified joint meeting, keep both participant feeds and the exact official episode title; otherwise route by the named host body only.
  Sample titles: Human Relations Commission - Mayor's Committee on Persons with Disabilities Human Relations Commission - Mayor's Committee on Persons with Disabilities.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-interim-report-of-the-task-force-on-race-and-culture`.** Keep the official report title. Route it to Public Briefings only if the City’s agenda identifies a public presentation; otherwise retain the archive record outside committee-meeting classification.
  Sample titles: Interim Report of the Task Force on Race and Culture Interim Report of the Task Force on Race and Culture.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-mayor-s-committee-on-persons-with-disabilites`.** Normalize the misspelling ‘Disabilites’ only as an input alias after the official agenda confirms the intended committee; preserve the original episode title.
  Sample titles: Mayor's Committee on Persons with Disabilites Mayor's Committee on Persons with Disabilites.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-mayors-committee-on-persons-with-disabilities`.** Treat ‘Mayors Committee’ as an alias for ‘Mayor’s Committee’ only where an official notice confirms identity.
  Sample titles: Mayors Committee on Persons with Disabilities Mayors Committee on Persons with Disabilities.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-mobility-infrastructure-and-transportation-committee-neighborhood-quality-and-revitalization-committee`.** Keep the two committees separate. When an exact same recording is an official joint meeting, place it in both participant feeds with its original title.
  Sample titles: Mobility: Infrastructure and Transportation Committee & Neighborhood Quality and Revitalization Committee Mobility: Infrastructure and Transportation Committee & Neighborhood Quality and Revitalization Committee.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-neighborhood-quality-and-revitalization-committee`.** For Fort Worth, match the exact Neighborhood Quality and Revitalization Committee title family, removing only date/time metadata. Do not use broad neighborhood or revitalization terms.
  Sample titles: Neighborhood Quality And Revitalization of August 9, 2022 Neighborhood Quality And Revitalization of August 9, 2022.
  Evidence status: Official exact-date agenda matches; verify source-player binding before applying
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-park-advisory-board-meeting`.** Use the officially documented board-name transition as an alias only for matching. Keep the 2020 title, date, and recording unchanged.
  Sample titles: Park Advisory Board Meeting of June 24, 2020 Park Advisory Board Meeting of June 24, 2020.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-park-and-recreation-advisory-board-public-hearing`.** Match the exact Board name plus meeting/public-hearing suffixes. Do not route it to Community Public Input unless notice shows a separate resident-comment event/participant.
  Sample titles: Park and Recreation Advisory Board Public Hearing Park and Recreation Advisory Board Public Hearing.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-park-board`.** Match ‘Park Board’ to the current official board only where dated City records establish the same body; preserve original titles.
  Sample titles: Park Board Park Board.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-pedestrian-and-bicycle-advisory-committee`.** Use one exact-body feed only after City appointment/charter records confirm that ‘Advisory Committee’ and ‘Advisory Commission’ refer to the same body. Preserve source titles.
  Sample titles: Pedestrian and Bicycle Advisory Committee Pedestrian and Bicycle Advisory Committee.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-race-anc-culture-task-force`.** Normalize this exact source-scoped misspelling to the documented Task Force name after verifying the recording’s official notice; preserve the original title.
  Sample titles: Race anc Culture Task Force Race anc Culture Task Force.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-race-and-culture-task-force-worksession`.** A City-created temporary task force with a formal resolution, named mandate and archived proceedings may be recognized as one finite historical body family. Do not include sponsored outreach or Council briefings just because they mention the task force.
  Sample titles: Race and Culture Task Force Worksession Race and Culture Task Force Worksession.
  Evidence status: Official City resolution and final report establish the finite body and mandate; exact recordings fit a source-scoped task-force rule, with outreach and Council reports kept separate.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-special-called-community-action-partners-cap-council`.** Match the exact CAP Council name and documented special-called suffix; do not include CAP service events.
  Sample titles: Special Called Community Action Partners (CAP) Council Special Called Community Action Partners (CAP) Council.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-task-force-meeting-by-task-force-on-race-and-culture`.** A City-created temporary task force with a formal resolution, named mandate and archived proceedings may be recognized as one finite historical body family. Do not include sponsored outreach or Council briefings just because they mention the task force.
  Sample titles: Task Force Meeting by Task Force on Race and Culture Task Force Meeting by Task Force on Race and Culture.
  Evidence status: Official City resolution and final report establish the finite body and mandate; exact recordings fit a source-scoped task-force rule, with outreach and Council reports kept separate.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-water-and-wastewater-cip-citizens-advisory-committee`.** Match the full water/wastewater capital-improvements citizens committee name and confirmed abbreviations only; do not match general water projects or transportation CIAC.
  Sample titles: Water and Wastewater CIP Citizens Advisory Committee Water and Wastewater CIP Citizens Advisory Committee.
  Evidence status: Official source identifies the body or meeting type; exact historical recording match remains open.
- **1 patterns / 2 source/UID pairs — `fort-worth-tx-water-utility-task-force`.** Add an exact source-scoped Water Task Force alias to a parent water advisory feed only when the City notice confirms the same institution; do not match water-project topics.
  Sample titles: Water Task Force Water Task Force.
  Evidence status: Case-specific title proposal; official source/event binding still needed
- **1 patterns / 1 source/UID pairs — `fort-worth-tx-housing-and-economic-development-committee-1`.** Match the source-scoped official body name 'Housing and Economic Development Committee #1 Housing and Economic Development Committee #1', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Housing and Economic Development Committee #1 Housing and Economic Development Committee #1.
  Evidence status: Preliminary title-only proposal; official evidence still needed
- **1 patterns / 1 source/UID pairs — `fort-worth-tx-housing-and-economic-development-committee-2`.** Match the source-scoped official body name 'Housing and Economic Development Committee #2 Housing and Economic Development Committee #2', allowing only case, punctuation, documented alias, and date/time suffix normalization. Preserve the official episode title; do not match topic words alone.
  Sample titles: Housing and Economic Development Committee #2 Housing and Economic Development Committee #2.
  Evidence status: Preliminary title-only proposal; official evidence still needed

### Addison


## What remains before any of these become feed rules

1. Decide which candidate bodies deserve their own feed and which belong in an already approved family.

2. For a confirmed public body, verify its official name, purpose, and whether it is continuing or tied
   to one program/cycle. Keep different bodies separate even when they discuss the same topic.

3. Match each historical recording to its dated City agenda/player. A title pattern alone cannot prove
   which recording is in a provider view, especially for joint videos or repeated labels.

4. Test the rule against nearby unrelated titles, then replay it across the full source. Future titles
   should match the rule; individual UIDs should remain evidence, not selector logic.

5. Only after maintainer approval and a bounded implementation contract should a new feed selector be
   added. The 14 Denton Special Citizens Bond Advisory patterns are proposed as a recurring-cycle rule;
   the Bond Oversight rule is already in the working tree as requested.
