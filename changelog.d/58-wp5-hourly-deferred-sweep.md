- **Review/58 WP5: hourly deferred sweep.** `llm-deferred-sweep` now runs every hour (minute :57)
  instead of every six hours, so completed LLM results are collected sooner. The concurrency group
  still never cancels a running sweep, and the sweep has a 45-minute wall-clock budget (script
  budget 32 min, step timeout 41, job timeout 45); hitting the budget exits successfully with the
  remaining work left deferred. Adds about 20 terminal-feed/poll calls a day.
