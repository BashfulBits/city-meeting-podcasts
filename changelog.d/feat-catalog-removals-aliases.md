- **Review/48 `/apply` removals and last-route alias proposals.** A ticked `remove route` on a
  route that became paid is no longer deferred as "requires Slice 3 rescue": with a fresh,
  uncontended, unchanged `not_entitled` proof it removes the route and repairs the affected lanes,
  or is rejected when no free replacement would remain. When the removal leaves a model with no
  route, the PR body lists the lanes that name it, warns that queued jobs naming it fail as
  `route_retired`, and proposes same-family or lane-sharing alias targets with a `model_aliases`
  snippet. The alias itself is still a reviewed edit.
