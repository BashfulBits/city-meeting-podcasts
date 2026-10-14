- Add an explicitly approved, evaluation-only Groq mode that continues GPT-OSS and Qwen
  without local daily-token gates until each route's first new provider HTTP429 or completion.
  Saved pauses survive restarts; request/minute limits, retry bounds and actual result usage
  remain. Production routes, quotas, records and audio are unchanged.
