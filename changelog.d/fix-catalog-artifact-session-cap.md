### Fixed

- **Daily provider-catalog run no longer crashes on its 4th measurement session.** The discovery
  evidence artifact still enforced the 3-session manual cap while the time-bound daily run may
  start up to 16; the shared cap is now `MAX_ARTIFACT_SESSIONS`. A failed evidence write no longer
  relabels a finished session as stopped, and no longer loses the run's state and issue update.
