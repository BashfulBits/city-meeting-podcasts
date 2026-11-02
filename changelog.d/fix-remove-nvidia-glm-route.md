- **Removed the NVIDIA GLM 5.3 Flash route.** NVIDIA's hosted endpoint never answered: a one-token
  prompt returned 504 after 302 s and all 16 production calls on 2026-11-02 hit the Worker's
  720 s timeout, each holding an NVIDIA lease that also kept the catalog run's pause from
  draining. The model stays available through the other GLM 5.3 Flash routes.
