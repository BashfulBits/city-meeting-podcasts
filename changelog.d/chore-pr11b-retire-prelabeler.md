- **The R5 pre-labelers and calibration matrix are retired (review/53 PR11b).**
  `llm_evaluation.py`, `llm_tag_review.py`, the `llm-evaluation` command, the `r5` issue handler,
  the `llm-tag-review` workflow, the pre-labeler batching/prompt in `tags.py`, its stage code,
  per-run caps and telemetry, the `topic-tags:prelabeler` and `-shadow` lanes (reservations now 8
  lanes), `tagging.evaluation` and `tagging.prelabeler` config, and the reconcile script's
  pre-labeler lane are removed. Display is unchanged: an LLM tag stays hidden and a rule tag
  shown (`tags.apply_admission`), checked against the old code on all 172,526 stored candidates
  with 0 differences in display, admission and `candidate_id`. `TAG_ADMISSION_POLICY` freezes the
  old matrix digest so no episode's tag fingerprint changes and nothing is re-opened.
