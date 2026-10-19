### Fixed

- Judge stack: a chat answer to a packet sent before `reason` was required, and with no reason,
  is no longer recorded as an unsupported judgment. It could never pass the quote check, and
  its row closed the question so the current prompt never asked it again; it is now dropped
  (`legacy_no_reason_dropped`) and asked again. Run 39264396053's 73% invalid quotes were 77%
  this case; the quote check itself was not too strict.

### Changed

- Judge stack: the run summary counts an answer with no reason (`quote_missing`) apart from a
  reason whose quote is absent from the evidence (`quote_invalid`). Pending markers record the
  reply contract they were sent under.
