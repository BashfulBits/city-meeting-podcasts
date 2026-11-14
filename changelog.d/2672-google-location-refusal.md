- **Google location refusals no longer block the judge anchor for six hours (#2672).** Google's
  HTTP 400 "User location is not supported for the API use." on any Gemini route is now
  `upstream_capacity` (`google-location-unsupported`). The attempt is retryable and the route takes
  the seconds-scale Google cooldown. Repeated refusals climb the route health ladder. Before, it
  was a withdrawn Decisions model (`route_unavailable`, a 6 h block on both anchor routes) or a
  failed chat job. Every refusal logged came from the Gateway's HKG colo. A genuine
  model-unavailable 400 still blocks the route.
