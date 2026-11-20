- **Provider catalog: added routes leave the list; `/apply` retries a busy re-check.** A route that
  lands on main no longer lingers in the rolling issue with a lone `ignore` box. The `/apply`
  re-check now retries a rate-limited or timed-out route twice (20 s, 60 s) before deferring it,
  and a deferral says what the re-check saw instead of "no current test result".
- **`/apply` adds a rate-limited route on its stored proof.** When the re-check still gets an HTTP
  429 or spent quota after its retries, a stored JSON check from the last 28 days stands (catalog
  values are still re-compared); the PR notes it. A 5xx, timeout, 401/402/403 or 404 still defers.
