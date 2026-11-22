- **Daily slice of configured-route cap re-measurement (review/54 §8.14.G).** The daily
  incremental run re-measures up to six caps in 40 minutes on providers it already visits, for
  routes the day's health check proved. Each session has a hard call cap of 10% of the route's
  daily request limit and of the provider's shared pool, less what the slice already spent that
  day (saved by UTC day, so a repeated run continues from it); where nothing publishes a daily
  limit the provider gets 20 requests. No session starts without enough calls to finish, and a
  gap in the verified history disables the slice for that run. Its evidence counts as history
  for the weekly review, which still makes the offers.
