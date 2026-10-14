- **Discovery generation-prompt floor.** Output and combined recovery preserve enough charged
  input for the probe prompt when shrinking output. Small reservations safely omit optional
  padding that cannot fit, and fixed-input refusals stop at the floor. Access errors defer
  without repeated probes. Typed free-tier prompt-size errors guide smaller input without
  inventing a numerical cap. No production caps or runtime-job margins change.
  Combined probes accept length replies generating at least the requested output, preserving
  actual counts separately without inferring a maximum from overfilled responses.
