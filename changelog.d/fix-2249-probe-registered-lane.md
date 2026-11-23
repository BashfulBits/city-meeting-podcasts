- **Queue latency probe uses a registered lane and reports why a submission is refused (#2249).**
  The Worker refuses `city-onboarding` jobs as `purpose_not_registered` because that purpose is
  deliberately non-dispatching, so the first probe run measured nothing. The probe now defaults to a
  registered lane (`topic-tags:tagger`, a `purpose` input on the workflow), records the Worker's
  rejection reason, and review/54 §8.15.G notes that queueing onboarding needs a registered lane.
