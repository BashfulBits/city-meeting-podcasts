### Fixed

- Review/51 P3 daily evidence collection (#2280): `remedy-p3-daily.yml` now passes the Granicus
  and Swagit proxy secrets that the feed audit uses, and `audit_feeds.py
  --unexpected-body-evidence` exits non-zero (after writing the file) when any source is
  unreachable. Previously unreachable providers were dropped silently and every run reported
  "0 findings" without ever seeing a source.
