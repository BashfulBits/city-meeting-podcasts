- **Size rejections overflow per job, not per route.** A provider "input too long" answer (or one
  request larger than the whole minute quota) now records the rejecting route on the job
  (`size_excluded_routes`) and requeues it onto a sibling route of the same model without spending
  the 5xx retry or standing the route down for other jobs. If every route rejects it, it fails
  with `input_too_large` for the producer to re-pack. Review/55 piece 4.
