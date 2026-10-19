- **A reply cut off at its output limit now records the cap it carried.** For
  `output_budget_exhausted`, `route_failures` keeps the `max_tokens` the request was sent with
  (`last_max_tokens_sent`), the tokens the reply held (`last_output_tokens`) and the day's largest
  such reply (`max_output_tokens`), shown in `/v2/stats?detail=1`. Nemotron Ultra chapter-agenda
  calls were cut off on 2026-10-18 (40 of 51 on NVIDIA) although agenda jobs ask for the route's
  full 65,536-token limit; this says whether the model really ran to that limit or a lower one
  applied.
