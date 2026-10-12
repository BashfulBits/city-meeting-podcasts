- **New-route probes separate API capacity from retrieval quality.** Successful ordinary chat
  completions with positive, consistent provider-reported input/output/total usage can establish
  admitted input sizes even when sentinel retrieval fails. Sentinel accuracy remains separately
  visible in artifacts and issues; malformed usage, refusals and errors do not establish capacity.
  Manual discovery adds an all/input/output selector so output can resume in its own bounded
  session. Existing histories remain readable, and configured-route calibration, production
  dispatch, budgets, provider pauses and activation gates are unchanged.
