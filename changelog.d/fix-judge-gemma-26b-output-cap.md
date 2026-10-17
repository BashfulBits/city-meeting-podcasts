- **Gemma 4 26B judge packets send a 4,096-token output cap.** A replay on 2026-10-17 reproduced
  the production failure: 26B falls into a repetition loop on mixed-kind packets and runs to the
  length limit (2 of 2 replies; 25 to 40% of its AI Studio calls today hit the 32,768-token limit
  and held a key slot up to 12 minutes). Its sibling packets now carry `max_tokens: 4096` instead
  of the route's own limit, so a loop ends in about 90 s. Replies are 900 to 1,200 tokens. Other
  sibling models are unchanged.
