- **Gemma models are family `gemma`, not `google`** (review/60 section 2, maintainer decision
  2026-11-17). `llm_families` now labels `google/gemma-4-31b-it` and `google/gemma-4-26b-a4b-it`
  `gemma`; Gemini models stay `google`, so no Gemini model can sibling the Gemini Decision anchor.
  Behaviour change: a Gemma-produced entry is now anchored by Gemini Decision 1 (before, by
  Perplexity Decider), a Gemma model is no longer barred as a sibling by the anchor's family
  (neither Gemma is in the dispatched `judge:sibling` lane, so no lane changes), and adjudication
  and the agreement report pair a Gemini anchor reading with a recorded Gemma sibling reading.
