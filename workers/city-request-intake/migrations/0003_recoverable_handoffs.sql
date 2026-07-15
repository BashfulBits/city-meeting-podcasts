ALTER TABLE city_requests ADD COLUMN payload_json TEXT;
ALTER TABLE city_requests ADD COLUMN request_marker TEXT;
ALTER TABLE city_requests ADD COLUMN last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_city_requests_pending
  ON city_requests(status, updated_at);
