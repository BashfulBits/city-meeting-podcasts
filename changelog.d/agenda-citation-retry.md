Chapter agenda: when most items in an extraction fail source verification (quote not in the cited
lines, line range outside the agenda), retry once with a message that names the failure classes
and the real line bounds, then stop (`retry_exhausted`) instead of re-rolling the same request
every run. The retry carries its hint in the recipe, so it is a new lineage and is not blocked by
the failed recipe's terminal-failure marker. The census now counts episodes blocked on agenda text
by reason (`no-agenda-link`, `agenda-rejected-<reason>`, `agenda-fetch-failed`,
`agenda-not-yet-fetched`) so the ~6,100 blocked episodes can be sized. No recipe changes for
episodes that do not fail.
