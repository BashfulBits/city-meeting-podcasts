- **Legistar auxiliary agendas for Swagit video feeds.** Relaxed `granicus_base` requirement in
  `LegistarProvider` to enable Legistar as an auxiliary agenda/calendar companion for non-Granicus
  video providers (Roadmap R11). Supported extracting Swagit video IDs (`/videos/(\\d+)`) into
  `AgendaRecord.video_guid` for Priority 1 GUID-based joining with Swagit episodes, populated
  clean canonical video URLs, and configured Dallas, TX (`dallas-tx`) to pair its Swagit video
  feeds with its official Legistar calendar.
