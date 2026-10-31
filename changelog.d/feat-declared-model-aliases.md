- **Declared model aliases.** `config/provider_limits.yml` accepts a `model_aliases` section
  (`old/id: {to, reason, decided_on}`) that keeps a retired model ID resolving to a newer model
  with at least one route, so queued and future jobs naming it are served instead of failing as
  `route_retired` when its last route is removed. The compiler rejects an alias whose source still
  has routes or whose target has none. The Worker needed no change: it already canonicalizes
  allowed models through `model_aliases`.
