- **Chapter locator fallback routing.** Keep the size-selected DeepSeek or MiniMax primary, and
  allow size-eligible alternatives and Kimi K3.1 Pro as Worker backups after repeated failures or
  when all primary routes are excluded, including a content-filter refusal. A 6,144-token admission
  reservation fits Kimi's configured maximum; `route_max` still sends the selected route's own
  output limit. The Kimi long-meeting probe timed out, so its reliability remains unverified.
  Queue accounting now conservatively budgets three model indexes instead of one. No prompt or
  pipeline version changes: completed compatible locator artifacts remain reusable; previously
  pending jobs retain their old policy until they are re-planned or re-enqueued. The changed lane
  fingerprint reopens retry-exhausted locator work, without intentionally invalidating completed
  artifacts.
