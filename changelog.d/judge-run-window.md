- **The judge lane's wall-clock window is its cron interval (120 minutes), not 90.** On
  2026-10-16 a catch-up pass spent its whole 76.5-minute window collecting 11,688 answers and
  stopped planning before it submitted anything. The window is now 120 minutes x 0.85 = 102,
  leaving about 18 minutes of the two-hour cadence for setup and the persistence tail. The
  run-history record used a 240-minute fallback for this lane; both now read the same value.
