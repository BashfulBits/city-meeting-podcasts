- **Provider catalog: filter duplicate physical routes and move legacy Nemotron aliases.** When
  upgrading routes, legacy model selectors are maintained under `model_aliases` rather than as
  duplicate route entries. Stale health routing and route health checks now deduplicate by physical
  tuple (`provider`, `account_id`, `upstream_model`), preventing duplicate probe reservations for
  uncompiled alias route IDs on the Worker dispatch rate ledger.
