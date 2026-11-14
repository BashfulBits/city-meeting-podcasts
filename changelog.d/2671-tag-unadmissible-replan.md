- **The tagger re-plans tag requests the Worker failed as too large (#2671).** On 2026-11-12 the
  Worker's oversize reaper failed about 330 queued jobs, tag batches among them, as `unadmissible`
  (over every live route's learned ceiling). The tagger rebuilt the same deterministic recipes on
  every run, the structural recovery gate refused each one, and the tag stage reported about 204
  errors and queued 0 to 1 jobs a run from 11-13. A refused recipe whose marker says
  `unadmissible` is now split in two under new child recipes (at a chapter boundary, or a single
  chapter's transcript segments halved), recursively; a single segment that is still refused is
  an oversized deferral. Other gate refusals (an exhausted retry budget) still raise.
