- **Review/58 LLM pipeline throughput balance (docs and evaluation tooling).** Seven days of Worker
  logs, AI Gateway analytics, GitHub run history and B2 state show the dispatcher is bound by how it
  holds slots: 58% of held slot-time waits for a bundle's slowest call and 22% runs to the 720 s
  timeout. The review ranks the bottlenecks, gives per-lane eligibility funnels, a timeout
  recommendation, saturation SLOs and a sequenced work-package list. It also scores six more
  high-context routes for chapter-locator packets above 76,000 estimated tokens and records the
  decision to move that band to MiniMax M3.1 Flash (88.0% on the 12 long meetings). The locator
  evaluation runner gains a `--pause-scope route` option and output limits for the new models. No
  production code or configuration changes.
