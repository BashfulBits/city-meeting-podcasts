- **Second BeatAPI account.** Add `BEATAPI_API_KEY_SECONDARY` as the `beatapi_secondary`
  provider with the five chat routes at the primary's limits (1 call/min, single-flight, no
  top-up), doubling the BeatAPI free-chat capacity that the DeepSeek V4/V4.1/V4 Pro and GPT-6
  pools share. JEV stays on the primary account, which chat load no longer competes with.
  Requires the Worker secret.
