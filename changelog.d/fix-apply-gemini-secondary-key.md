- **`/apply` can now re-check routes on the second Gemini account.** The apply workflow had
  `GEMINI_API_KEY` but not `GEMINI_API_KEY_SECONDARY`, so a re-check of a secondary-account Gemini
  route could not authenticate. The reconcile workflow already had both.
