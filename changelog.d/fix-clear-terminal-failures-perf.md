- **Terminal failure cleanup resilience and performance.** Optimize `clear_terminal_failures.py`
  with `--days` time cutoff (default 7 days) and 8-worker concurrent downloads via
  `ThreadPoolExecutor`, safe error handling for transient storage exceptions, and real-time progress
  logging with unbuffered workflow output in `.github/workflows/clear-terminal-failures.yml`.
