- **Granicus proxy fallback and stderr diagnostics for video slides.** Wire Cloudflare Worker
  media proxy fallback into `SlidesStage` for video probe, detection, and frame capture when
  Granicus returns HTTP 403 Forbidden to GitHub Actions runners. Include redacted stderr in
  subprocess error logs and reset failed attempts on dirty slide records so that previously failed
  episodes are retried. Add `scripts/reset_slides_backoff.py` for targeted state recovery.
