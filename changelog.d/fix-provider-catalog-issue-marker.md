- **Provider catalog issue keeps its marker.** When the machine state filled the issue body, the
  report truncation dropped the leading marker, so `/apply` and the issue lookup silently skipped.
  The marker is now always kept and display-only observations are shed from the state first.
