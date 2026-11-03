- **Renovate runner upgrade suppression.** Pinned GitHub runner environments (`runs-on`) to
  prevent Renovate from proposing automated Ubuntu LTS upgrades across workflow files, preserving
  the ABI alignment required by the self-built static FFmpeg binary (review/22).
