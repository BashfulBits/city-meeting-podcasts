- **Google Gemma-31B Gateway retries.** Send `cf-aig-max-attempts: 1` on both Google
  Gemma-31B project routes so the dispatch worker owns paced retries and sees provider failures.
  Spaced replay tests found quota refusals with default Gateway handling but none directly or
  with an explicit one-attempt header. Add validated route-level catalog overrides and retain
  provider defaults for other routes. Quotas, pacing, packing, request bodies, deadlines, and
  worker retry budgets remain unchanged. No pipeline version bump or artifact backfill.
- **CI FFmpeg compatibility.** Pin the test job to Ubuntu 24.04 so the checksum-pinned FFmpeg
  build can install its required `libvpx9` runtime during GitHub's `ubuntu-latest` migration.
  Production runner images and the FFmpeg binary/checksum remain unchanged.
