- **Replace raw invisible Unicode characters with explicit escape sequences.** Converted literal
  zero-width spaces (`\u200b`) in `availability_digest.py`, `test_availability_digest.py`, and
  `check_endpoints.py` to explicit `\u200b` escapes, replaced literal `\uFEFF` in
  `structured_output.js` with explicit `\uFEFF`, and removed an invisible zero-width space in
  `review/05-endpoint-contract-tests.md`. This clears Renovate's hidden Unicode security warning.
