- **A re-run of city onboarding or the remedy classifier is no longer stuck on an old failure.**
  The Worker keeps a permanently failed queued job under its recipe hash and replays that failure
  to every later submission, so a retry after a transient cause (a full route) failed again at
  once. The interactive wait now retries a permanent failure once under a new recipe hash.
