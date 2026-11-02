- **A provider that cannot drain no longer wastes the daily measurement.** The 2026-11-02 manual
  run chose MiniMax on NVIDIA, but NVIDIA's dispatch pause never drained: its judge calls hang
  until the Worker's 720 s timeout, longer than the 600 s drain wait, so the run recorded a
  misleading "bounded slot unavailable, rechecked next week" and measured nothing. The drain
  wait is now 780 s, the daily plan names one candidate per provider plus two fallback providers
  so a contended provider spends no session and the next one is measured, and the deferral now
  says the pause was contended and that the next daily run retries.
