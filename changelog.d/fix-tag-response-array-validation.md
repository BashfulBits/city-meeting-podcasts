- **Tag structured output array normalization.** Accept bare JSON array responses and single-key
  wrapped lists from structured LLM outputs in `llm_tag_suggestions`, preventing schema validation
  failures when models like `gemini-3.1-flash-lite` return top-level tag lists directly.
