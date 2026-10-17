- **Admission and packing use different percentiles.** The size gates (configured ceiling, learned
  bounds, Gemini minute quota, structural check) now use an admission ratio, p75 x 1.05 and no
  longer floored by the route prior; reservation and TPM accounting keep the conservative ratio.
  `GET /v2/calibration?model=` publishes the largest packet the smallest route of a model will
  admit (p95 packing ratio) and the judge sizes sibling packets to it. Review/55 piece 2.
