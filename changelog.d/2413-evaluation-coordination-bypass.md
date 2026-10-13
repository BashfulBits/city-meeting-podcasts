- Add an explicit default-off coordination bypass for the existing review/51 evaluations
  during the Worker database-read outage. Preserve local quota accounting, original
  deadlines, retry limits and one shared call lock; hold Qwen without verified capacity.
  Retain the loss of production-usage visibility in immutable attempt provenance.
