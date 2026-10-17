- **Gemma 4 26B is removed from the judge sibling lane.** On 2026-10-17 fewer than 40% of its
  AI Studio judge replies were usable: it falls into a repetition loop on mixed-kind packets and
  runs to the 32,768-token limit (a direct replay reproduced it in 2 of 2 replies, and a capped
  4,096-token reply still failed to parse), holding a key slot up to 12 minutes. It stays eligible
  (not dispatched) and the Google slot is Gemma 4 31B alone. Packets already queued for 26B still run.
