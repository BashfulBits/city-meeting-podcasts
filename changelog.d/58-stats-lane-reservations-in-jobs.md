### Changed

- **`/v2/stats` reports lane reservations in jobs** (review/58 §12.11).
  `ingress.by_purpose[].daily_quota` is now each lane's own reservation in jobs, not the shared
  write-unit ceiling divided by units per job, which made every lane look like most of the day
  and summed to about 40,500 against a plan of 4,494. `percent_used` is the lane's charged write
  units over its reserved units (over 100% means the lane is borrowing). New fields:
  `ceiling_jobs` per lane (the most it can reach by borrowing) and `ingress.reserved_jobs_total`.
