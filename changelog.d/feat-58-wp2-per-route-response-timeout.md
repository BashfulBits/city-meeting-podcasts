- **Per-route response timeout (review/58 WP2).** `config/provider_limits.yml` routes may set
  `response_timeout_seconds` (positive integer, at most `MAX_RESPONSE_SECONDS`, validated by the
  compile script); the dispatch Worker uses the smaller of it and the global 720 s for every call
  on that route, with the same `provider_response_timeout` failure class and unchanged requeue,
  retry and lease handling. Set on AI Studio Gemma 4 31B (240), Gemini Flash Lite (300), Nemotron
  3.5 Super and DeepSeek V4 Flash (420). `bundle_completed` calls now carry `response_timeout_ms`.
