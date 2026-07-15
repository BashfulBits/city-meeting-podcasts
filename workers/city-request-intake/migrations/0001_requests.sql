CREATE TABLE IF NOT EXISTS city_requests (
  fingerprint TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  city_name TEXT NOT NULL,
  state TEXT NOT NULL,
  issue_number INTEGER,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_city_requests_issue ON city_requests(issue_number);
