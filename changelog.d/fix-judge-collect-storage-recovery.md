- **Judge waits out a storage blip.** After three unreadable pending-packet records in a row, the
  judge pass now probes the same record every 30 seconds (at most 300 seconds per pass) instead of
  skipping every remaining record. Run 38984100770 lost a whole pass of collection (2,449 of
  2,452 markers skipped, 27 judgments) to a blip of about five minutes. If storage is still down
  after the budget, the rest keep their markers and the pass goes on.
