- **More configured-route cap re-measurements per weekly run (review/54 §8.14.G).** The weekly
  run's ceiling was the limit, not its clock: it re-measured 4 of 92 due axes in 97 of 360 minutes.
  The session ceiling goes from 4 to 12, the re-measure window from 1 to 3 hours, and the evidence
  artifact from 16 to 32 sessions, with a stop before the artifact could pass its 2 MiB limit. A
  bracket already measured tightly around the configured cap is re-tested in two calls instead of
  searched again, and a refusal there falls back to the full search. The rolling issue now reports
  session time, provider calls, confirmed brackets and the reasons axes waited.
