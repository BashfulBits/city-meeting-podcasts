- **Judge survives a storage read outage.** A `StorageReadUnavailable` while reading a pending
  packet's record no longer aborts the judge lane (run 38971148564 failed 29 minutes in this way,
  before it could plan or submit). The marker stays, the packet counts as outstanding, and a later
  pass collects it; after three unreadable records in one pass the rest are not tried.
