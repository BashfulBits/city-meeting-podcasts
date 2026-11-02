- **The dispatch Worker streams BeatAPI replies.** BeatAPI's Cloudflare edge ends a non-streaming
  call at about 125 s with a 524 (3 of 3 long test calls failed; 17 of today's BeatAPI GLM calls
  did), while streamed calls kept data flowing for 7+ minutes and a 415 s reply completed. A
  provider with `stream_response: true` (a route can override it) now gets `stream: true`; the
  Worker rebuilds the single reply (content, reasoning, tool calls, finish reason, usage) so
  nothing downstream changes, fails a stream that goes quiet for 60 s or ends early, and caps the
  text it will hold. Enabled for both BeatAPI accounts. To revert, delete the line from the
  provider block in `config/provider_limits.yml` and run `scripts/compile_llm_limits.py`.
