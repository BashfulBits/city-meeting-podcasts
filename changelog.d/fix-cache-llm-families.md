- **Judge no longer spends its first 12 minutes re-parsing the site config.** The dirtiness check
  asked `load_families()` for the family map once per candidate episode and each call re-parsed
  the 60 KB site config (25,283 episodes, about 0.06 s each locally, ten-plus minutes on the
  runner) before any judging began. It is now cached per path like `load_lanes()`.
