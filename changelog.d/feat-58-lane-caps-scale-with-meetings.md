- **LLM lane reservations are split from the daily row cap by ratio (review/58 section 12.11).**
  Each lane in `config/site_config.yml` now states only a `reservation_ratio` (agenda 10, locator
  10, tagger 40, moments 10, anchor 20, sibling 30, adjudicator 10). Live lanes (`live: true`,
  today `remedy:p3`) share a fixed `live_reservation_jobs` pool of 100 jobs by ratio, taken off
  the top. `scripts/compile_llm_lanes.py` and the client's `load_lanes` split the rest of
  `DO_ROWS_ENQUEUE_STOP` (90,000, now declared in `config/dispatch_tuning.yml`) with the
  `llm_lane_budget` row model: 4,494 reserved jobs and 23,046 units (was 670 units). Every lane's
  `daily_write_units` is the reserved total and `max_queued_jobs` two days of its reservation.
  Adding or removing a lane re-splits the reservations with no other edit. The v2 Worker clamps
  its ingress budget to the reserved total, so lanes exceed their share only by borrowing unspent
  reservations later in the UTC day. The compiled `ingress_reservations.json` changes, so merging
  deploys the Worker.
