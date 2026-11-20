- **Provider catalog: added routes leave the list; `/apply` retries a busy re-check.** A route that
  lands on main no longer lingers in the rolling issue with a lone `ignore` box. The `/apply`
  re-check now retries a rate-limited or timed-out route twice (20 s, 60 s) before deferring it,
  and a deferral says what the re-check saw instead of "no current test result".
