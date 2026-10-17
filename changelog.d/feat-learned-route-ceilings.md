- **Routes learn their real input limits from traffic.** Each route now keeps the largest input
  it accepted (from `usage.prompt_tokens`) and the smallest it refused for size, in provider
  tokens, expiring after 7 days. Admission skips a route for jobs at or above its refusal point
  (the drain pass re-tests it) and no longer lets a conservative configured ceiling reject a
  size the route has already served. Both show in `/v2/stats?routes=1`. Review/55 piece 3.
