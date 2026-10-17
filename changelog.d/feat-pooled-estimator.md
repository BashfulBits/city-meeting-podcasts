- **Input-ratio estimator pooled across routes, with a longer window and no prior floor.**
  Observations are pooled per (tokenizer family, prompt family) so every route serving a model
  shares one ring of 128 samples, not 32 per route. Percentiles are used from 64 samples; from 16
  to 63 they are blended with the route prior, and from 64 the prior is a seed, not a floor
  (reservation = max(p95 x headroom, p99); admission p75; packing p95). Output reserve gets the
  same treatment. Review/55 piece 1.
