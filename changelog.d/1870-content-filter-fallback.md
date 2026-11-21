Treat HTTP 200 content-filter refusals as job-specific policy failures. Retry eligible alternatives
without reusing the refusing route, preserve reported token usage, and surface refusals in the
LLM budget monitor. Jobs with no permitted route fail with the existing policy terminal reason.
