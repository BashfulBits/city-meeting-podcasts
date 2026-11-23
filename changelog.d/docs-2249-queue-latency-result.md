- **Queue round trip measured for the #2249 design (review/54 §8.15.G).** The latency probe shows
  a queued job takes about 60 seconds at priority 0 and at priority 1 alike, because the Worker
  claims jobs on a one-minute cron and priority only orders jobs within a tick. One-call callers
  (city onboarding, interactive remedy) fit route A with a registered lane; the provider catalog's
  limit-measuring searches do not, and already coordinate through the dispatch pause.
  The probe's backlog readout now reads `jobs.by_state`.
