### Fixed

- Feed-health audit (#2779): agendas the publisher itself reports as unpublished (Granicus
  "This agenda is not currently published", served for older meetings that never had one) now get
  the `agenda-not-published` reason and no longer count toward the `agenda-quality` finding. Earlier
  assessments are recognised by the page's document hash. On production records this drops the
  issue from 60 feeds and about 2,650 rejected agendas to 13 feeds and 34.
