### Changed

- Dispatch Worker: Google AI Studio calls now go through the Worker's own fetch handler over a self
  service binding (`PLACED_EGRESS`), and the Worker carries a placement hint (`gcp:us-east4`), so
  they no longer leave from colos Google refuses (HKG: HTTP 400 "User location is not supported").
  The hop needs the ingress bearer token and only forwards to the AI Gateway or Google API hosts.
  `PLACED_EGRESS_PROVIDERS` (wrangler var, default `gemini`) lists the providers; empty turns it
  off. The ingress API also runs at the placed location now (#2682, review/58 12.12).
