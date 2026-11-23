- **LLM queue latency probe (review/54 §8.15.G, #2249).** A manual `LLM queue latency probe`
  workflow submits tiny sequential `city-onboarding` jobs to the v2 Worker queue at priority 0 and
  priority 1, alternating so both see the same backlog, and reports submit time and total time to
  result per priority (plus Worker backlog before and after). It measures whether interactive
  callers could use the priority 0 lane instead of calling a provider directly. It spends a few
  free-tier provider calls.
