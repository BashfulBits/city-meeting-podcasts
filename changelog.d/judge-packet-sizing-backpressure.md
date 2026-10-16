- **Judge packets are sized as the Worker measures them, and the judge stops outrunning the queue.**
  JEV packets were sized by each item's text, so about 45 of 110 anchor packets a run exceeded the
  route's input ceiling once the serialized request was counted, were failed by the Worker and
  returned to the planner (the 43 to 52 `LLMBackendError` and about 1,500 re-planned items a run).
  Packets are now sized from the serialized questions and the Worker's learned input ratio. A pass
  also submits only into the room under `judging.max_outstanding_packets` (anchor 240, sibling 480),
  so its share of the Worker queue (8,485 jobs on 2026-10-15) cannot keep growing. The judge lanes
  join the ingress preflight, so a Worker that is down or refusing ingress costs no planning pass.
  Records the first PR4 acceptance pass in review/53.
