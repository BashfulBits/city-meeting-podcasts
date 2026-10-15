- **Provider-default remedy reasoning.** Remedy admissions can now explicitly record
  `provider-default` reasoning, meaning the request omits a reasoning override and hashes only
  the route's compiled request parameters. This supports the evaluated P3 configuration without
  guessing a named provider effort; runtime routing remains shadowed until the exact routes are
  admitted.
