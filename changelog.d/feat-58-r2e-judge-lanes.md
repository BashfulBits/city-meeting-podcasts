- **Judge sibling roster after Gemma (review/58 12.6).**
  - Gemma 4 31B leaves `judge:sibling`: a Google sibling can never judge beside the Google anchor,
    and 4% of its queued calls succeeded.
  - Nemotron 3.5 Super is slot `primary`. Qwen3.8-27B (Groq, family `qwen`) is slot `secondary`
    and judges the Nemotron-produced entries that previously had no sibling.
  - New `judging.max_outstanding_per_sibling` caps Qwen at 40 outstanding packets.
  - Pending markers for a sibling model the lane no longer lists are dropped
    (`retired_sibling_pending_dropped`). Stored Gemma judgments keep their identity.
  - Everything stays in shadow.
- **DeepSeek V4.1 Flash adjudication moves to BeatAPI (review/58 12.7).** The NVIDIA leg is stood
  down (`rpd: 0`, about 5% success). The two BeatAPI legs carry the model.
