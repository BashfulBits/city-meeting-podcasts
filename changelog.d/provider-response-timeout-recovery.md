- **Recover confirmed provider response timeouts.** Distinguish the executor's own deadline from
  unclassified transport failures; count explicit HTTP 504/524 timeouts too. After two deadlines
  on a physical route, try fitting permitted
  siblings, waiting for their capacity when necessary. Persist per-job counts without spending
  the 5xx allowance; preserve Google quota exclusions and charge unknown upstream consumption.
  A nullable coordinator field needs no payload rewrite. No deadline, request-shaping, quota,
  pipeline-version, or stored-artifact change; already-failed jobs are not revived.
