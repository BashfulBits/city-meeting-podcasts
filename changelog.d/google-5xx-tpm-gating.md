- **Gated Google 5xx in-window retries on TPM capacity and retained 5xx input accounting.** Prevents
  immediate HTTP 429 rate limit errors on Google AI Studio (`gemma-4-31b-it`) by skipping in-window
  5xx retries when repeating a prompt's input tokens would exceed the route's rolling 60-second
  TPM limit (`(inWindow5xxRetries + 2) * tokens <= routeTpm`). Retains input token window
  accounting on Google 500/503 responses that reached the provider (`actual_start_at != null`)
  in `settleInputWindow`, preventing subsequent jobs from being admitted before Google's 60-second
  arrival token quota has refilled.
