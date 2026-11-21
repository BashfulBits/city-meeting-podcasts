- **Video slide chapter images (GH#2707, review/61).** A new `slides` lane
  (`.github/workflows/slides.yml`, every 4 h) streams each meeting's video once through ffmpeg
  `freezedetect` (keyframes only, 320 px gray), captures one frame per static span of 15 s or more,
  filters holding slates, black or blank frames, soft camera shots, recurring frames and
  duplicates, and stores each slide as a content-addressed 1280×720 JPEG at B2
  `slides/<uid>/<hash>.jpg`. The Podcasting 2.0 chapters JSON puts the image on a matching agenda
  chapter or adds a `"toc": false` slide entry, mapped to the served clock at render. Episodes with
  slides but no agenda chapters now get a chapters document. **On by default**: a feed opts out with
  `slides: {enabled: false}`, and site `slides.enabled: false` is the kill switch. Feed-only:
  `feed_content_hash` changes once, so the first deploy re-renders every feed; `audio_spec_hash`
  and the M4A chapters are untouched, so nothing re-encodes. **Backfill:** a new stage at
  version 1 with no prior artifacts, so nothing is invalidated; the archive drains newest-first
  under `max_episodes_per_run: 20` and `max_video_gb_per_run: 40` per run. The orphan GC keeps
  referenced slide keys and manages `slides/*.jpg`.
