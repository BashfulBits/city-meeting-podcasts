- **City onboarding's queued classification may use its fallback models (#2249).** The first live
  run asked for Gemini 3.7 alone, which allows 20 requests a day per account, and waited the full
  ten minutes for capacity. It now lets the Worker choose among 3.7 and its three vetted fallbacks.
