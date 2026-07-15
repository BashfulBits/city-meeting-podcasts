CREATE TABLE IF NOT EXISTS request_origins (
  issue_number INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  discord_message_id TEXT,
  email_notification_key TEXT,
  discord_notification_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
