- **Gemini Decision 1 as the judge anchor (review/58 WP9; canonical in review/49).** BeatAPI
  retired free JEV on 2026-11-05, so `judge:anchor` now names `gemini/gemini-decision-1` in the
  same shadow state (no new switch, no added authority). The dispatch Worker gains a `decisions`
  API shape (`/v1beta/decisions`, `x-goog-api-key`, key mapping to Google's charset, no `criteria`
  on `noul`, body-classified 400s with quota reported as 429) and two routes on the existing Gemini
  keys at a 45,000-token ceiling. `DecisionBackend` packs for that ceiling and records answer
  confidence. Judge independence now covers the anchor: Google-produced or unknown-family entries
  get no anchor, and siblings and adjudicators exclude the anchor's family. Pending JEV anchor
  markers are dropped, and the judge report keys anchor statistics by model so JEV and Gemini
  telemetry are never pooled.
