### Changed
- Dispatched replies that fail local validation now carry a text-free diagnostic (JSON versus schema
  stage, finish reason, completion tokens, content length, schema-field error locations), saved on
  the terminal-failure marker as `diagnostic`. The old message lumped truncated or unparsable JSON
  under "failed Pydantic validation", so a lane's 3-attempt exhaustions could not be classified.
