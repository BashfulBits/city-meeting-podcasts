- **Review/48 cleaner `/apply` route assignment.** A candidate whose provider name is only a
  respelling of an existing pool (`z-ai/` vs `zai/`, `:free`/`-free` suffixes, a bare name under a
  namespaced pool) now resolves to that pool instead of deferring as `identity_required`; no new
  identity is ever invented. A route-only addition with a provider-published context size no longer
  needs provider-counted input evidence: it is added with a provisional 16k `hard_input_ceiling`
  and flagged for a later discovery run, and lane placement still requires measured evidence. Every
  addition PR body now carries a route to pool table with lane backups.
