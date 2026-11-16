Tagger jobs now rotate among their eligible routes after an input TPM refusal without spending
their normal retry budget. When all routes refuse, the tagger tries at most two smaller
generations and reports a distinct quota block if they cannot proceed. Existing completed work
is not invalidated.
