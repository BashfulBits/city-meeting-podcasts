- **City onboarding and the remedy classifier queue on one `interactive-classify` lane (#2249).**
  Both called Gemini directly, spending quota the Worker's ledger could not see. They now submit at
  priority 0 and wait for the result in the same run (about a minute per call), through
  `citypods/compute/interactive.py`. The lane is a live lane over the union of their routes (50 of
  the 150 live jobs a day), so the other lanes' reservations shrink by about 1%. A classification
  not finished in time defers to the next run. Limit-measuring searches stay direct.
